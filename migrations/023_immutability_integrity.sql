-- ════════════════════════════════════════════════════════════════════════════
-- Migration 023: Immutability + Integrity
--
-- 1. DB-level immutability on journal_entries (block UPDATE except reversal
--    fields, block DELETE entirely)
-- 2. DB-level immutability on journal_lines (block UPDATE + DELETE)
-- 3. fn_check_accounting_integrity() — callable consistency validator
-- ════════════════════════════════════════════════════════════════════════════

BEGIN;

-- ════════════════════════════════════════════════════════════════════════════
-- 1. JOURNAL ENTRIES — IMMUTABILITY TRIGGER
--
--    Only these columns may change (via reversal flow):
--      is_reversed, reversed_by, reversed_entry_id, source_event
--    Everything else is locked. DELETE is blocked entirely.
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_immutable_journal_entries()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Cannot delete journal entries — use reversal instead'
            USING ERRCODE = 'P0002';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        -- Allow ONLY reversal-related field changes
        IF OLD.restaurant_id    IS DISTINCT FROM NEW.restaurant_id
        OR OLD.branch_id        IS DISTINCT FROM NEW.branch_id
        OR OLD.entry_date       IS DISTINCT FROM NEW.entry_date
        OR OLD.reference_type   IS DISTINCT FROM NEW.reference_type
        OR OLD.reference_id     IS DISTINCT FROM NEW.reference_id
        OR OLD.description      IS DISTINCT FROM NEW.description
        OR OLD.created_by       IS DISTINCT FROM NEW.created_by
        OR OLD.created_at       IS DISTINCT FROM NEW.created_at
        THEN
            RAISE EXCEPTION 'Journal entries are immutable — only reversal fields may change'
                USING ERRCODE = 'P0002';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_immutable_journal_entries ON journal_entries;

CREATE TRIGGER trg_immutable_journal_entries
    BEFORE UPDATE OR DELETE ON journal_entries
    FOR EACH ROW
    EXECUTE FUNCTION fn_immutable_journal_entries();


-- ════════════════════════════════════════════════════════════════════════════
-- 2. JOURNAL LINES — FULL IMMUTABILITY (no update, no delete)
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_immutable_journal_lines()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Journal lines are immutable — cannot % on journal_lines',
        TG_OP
        USING ERRCODE = 'P0002';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_immutable_journal_lines ON journal_lines;

CREATE TRIGGER trg_immutable_journal_lines
    BEFORE UPDATE OR DELETE ON journal_lines
    FOR EACH ROW
    EXECUTE FUNCTION fn_immutable_journal_lines();


-- ════════════════════════════════════════════════════════════════════════════
-- 3. ACCOUNTING INTEGRITY CHECK FUNCTION
--
--    Returns a JSON result with checks:
--      - trial_balance_check: sum(debit) == sum(credit) globally
--      - entry_balance_check: every individual entry balances
--      - orphan_lines_check: journal_lines without parent entry
--      - broken_account_refs: lines referencing deleted/missing accounts
--      - unreversed_reversals: entries marked as reversal but original not flagged
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_check_accounting_integrity(p_restaurant_id UUID)
RETURNS JSONB AS $$
DECLARE
    v_result JSONB := '{}'::jsonb;
    v_total_debit NUMERIC;
    v_total_credit NUMERIC;
    v_unbalanced_count INT;
    v_orphan_count INT;
    v_broken_ref_count INT;
    v_unlinked_reversal_count INT;
BEGIN
    -- Check 1: Global trial balance
    SELECT COALESCE(SUM(jl.debit), 0), COALESCE(SUM(jl.credit), 0)
    INTO v_total_debit, v_total_credit
    FROM journal_lines jl
    JOIN journal_entries je ON je.id = jl.journal_entry_id
    WHERE je.restaurant_id = p_restaurant_id;

    v_result := v_result || jsonb_build_object(
        'trial_balance', jsonb_build_object(
            'passed', v_total_debit = v_total_credit,
            'total_debit', v_total_debit,
            'total_credit', v_total_credit,
            'difference', v_total_debit - v_total_credit
        )
    );

    -- Check 2: Per-entry balance (debit == credit for each entry)
    SELECT COUNT(*) INTO v_unbalanced_count
    FROM (
        SELECT jl.journal_entry_id,
               SUM(jl.debit) AS d, SUM(jl.credit) AS c
        FROM journal_lines jl
        JOIN journal_entries je ON je.id = jl.journal_entry_id
        WHERE je.restaurant_id = p_restaurant_id
        GROUP BY jl.journal_entry_id
        HAVING ABS(SUM(jl.debit) - SUM(jl.credit)) > 0.001
    ) sub;

    v_result := v_result || jsonb_build_object(
        'entry_balance', jsonb_build_object(
            'passed', v_unbalanced_count = 0,
            'unbalanced_entries', v_unbalanced_count
        )
    );

    -- Check 3: Orphan lines (lines without parent entry)
    SELECT COUNT(*) INTO v_orphan_count
    FROM journal_lines jl
    LEFT JOIN journal_entries je ON je.id = jl.journal_entry_id
    WHERE je.id IS NULL;

    v_result := v_result || jsonb_build_object(
        'orphan_lines', jsonb_build_object(
            'passed', v_orphan_count = 0,
            'orphan_count', v_orphan_count
        )
    );

    -- Check 4: Broken account references
    SELECT COUNT(*) INTO v_broken_ref_count
    FROM journal_lines jl
    JOIN journal_entries je ON je.id = jl.journal_entry_id
    LEFT JOIN chart_of_accounts coa ON coa.id = jl.account_id
    WHERE je.restaurant_id = p_restaurant_id
      AND coa.id IS NULL;

    v_result := v_result || jsonb_build_object(
        'broken_account_refs', jsonb_build_object(
            'passed', v_broken_ref_count = 0,
            'broken_count', v_broken_ref_count
        )
    );

    -- Check 5: Unlinked reversals (is_reversed=true but no reversed_by, or vice versa)
    SELECT COUNT(*) INTO v_unlinked_reversal_count
    FROM journal_entries je
    WHERE je.restaurant_id = p_restaurant_id
      AND (
          (je.is_reversed = true AND je.reversed_by IS NULL)
          OR (je.reversed_by IS NOT NULL AND je.is_reversed = false)
      );

    v_result := v_result || jsonb_build_object(
        'reversal_integrity', jsonb_build_object(
            'passed', v_unlinked_reversal_count = 0,
            'unlinked_count', v_unlinked_reversal_count
        )
    );

    -- Summary
    v_result := v_result || jsonb_build_object(
        'all_passed',
            v_total_debit = v_total_credit
            AND v_unbalanced_count = 0
            AND v_orphan_count = 0
            AND v_broken_ref_count = 0
            AND v_unlinked_reversal_count = 0
    );

    RETURN v_result;
END;
$$ LANGUAGE plpgsql;

COMMIT;
