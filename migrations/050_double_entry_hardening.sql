-- ============================================================================
-- 050_double_entry_hardening.sql
-- ----------------------------------------------------------------------------
-- Lock down `journal_entries` + `journal_lines` to true bank-grade behaviour:
--
--   1. APPEND-ONLY: no UPDATE, no DELETE on either table.
--      Adjustments must go through reversal entries (the existing
--      `is_reversed` / `reversed_by` columns on journal_entries).
--      `is_reversed` is the ONLY column the trigger allows to be flipped
--      (NULL → reversed_id), and only by setting it once.
--
--   2. MIN 2 LINES: a journal entry without at least two lines isn't a
--      double-entry — enforced at COMMIT via deferred constraint trigger.
--
--   3. The existing balance trigger (migration 006) already enforces
--      sum(debit) = sum(credit). We add a helpful safety net: a check that
--      the sums are non-zero (no all-zero "ghost" entries).
--
-- WHY: Stripe / Square / RBI auditors reject ledgers that allow:
--   * silent UPDATEs to debit/credit
--   * single-leg entries
--   * zero-sum filler rows
-- This migration makes those impossible at the storage layer.
-- ============================================================================

BEGIN;

-- 1) Append-only on journal_entries (allow only the reversal flip) -----------
CREATE OR REPLACE FUNCTION fn_journal_entries_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'journal_entries is append-only (DELETE denied for entry %)', OLD.id;
    END IF;
    -- UPDATE: allow flipping is_reversed from false→true and setting reversed_by once.
    IF NEW.id            <> OLD.id            THEN RAISE EXCEPTION 'id is immutable';            END IF;
    IF NEW.restaurant_id <> OLD.restaurant_id THEN RAISE EXCEPTION 'restaurant_id is immutable'; END IF;
    IF COALESCE(NEW.entry_date, '1900-01-01'::date) <> COALESCE(OLD.entry_date, '1900-01-01'::date) THEN
        RAISE EXCEPTION 'entry_date is immutable';
    END IF;
    IF NEW.reference_type <> OLD.reference_type THEN RAISE EXCEPTION 'reference_type is immutable'; END IF;
    IF COALESCE(NEW.reference_id,'') <> COALESCE(OLD.reference_id,'') THEN
        RAISE EXCEPTION 'reference_id is immutable';
    END IF;
    -- is_reversed: allow false→true once. Disallow true→false or true→true with new id.
    IF OLD.is_reversed = true AND NEW.is_reversed = true
       AND COALESCE(OLD.reversed_by, '00000000-0000-0000-0000-000000000000'::uuid)
        <> COALESCE(NEW.reversed_by, '00000000-0000-0000-0000-000000000000'::uuid) THEN
        RAISE EXCEPTION 'reversed_by is immutable once set';
    END IF;
    IF OLD.is_reversed = true AND NEW.is_reversed = false THEN
        RAISE EXCEPTION 'cannot un-reverse a journal entry';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_journal_entries_guard_upd ON journal_entries;
DROP TRIGGER IF EXISTS trg_journal_entries_guard_del ON journal_entries;
CREATE TRIGGER trg_journal_entries_guard_upd
BEFORE UPDATE ON journal_entries
FOR EACH ROW EXECUTE FUNCTION fn_journal_entries_guard();
CREATE TRIGGER trg_journal_entries_guard_del
BEFORE DELETE ON journal_entries
FOR EACH ROW EXECUTE FUNCTION fn_journal_entries_guard();


-- 2) Append-only on journal_lines --------------------------------------------
CREATE OR REPLACE FUNCTION fn_journal_lines_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'journal_lines is append-only (DELETE denied for line %)', OLD.id;
    END IF;
    RAISE EXCEPTION 'journal_lines is append-only (UPDATE denied for line %)', OLD.id;
END $$;

DROP TRIGGER IF EXISTS trg_journal_lines_guard_upd ON journal_lines;
DROP TRIGGER IF EXISTS trg_journal_lines_guard_del ON journal_lines;
CREATE TRIGGER trg_journal_lines_guard_upd
BEFORE UPDATE ON journal_lines
FOR EACH ROW EXECUTE FUNCTION fn_journal_lines_guard();
CREATE TRIGGER trg_journal_lines_guard_del
BEFORE DELETE ON journal_lines
FOR EACH ROW EXECUTE FUNCTION fn_journal_lines_guard();


-- 3) Min 2 lines + non-zero sums (deferred until commit) ---------------------
CREATE OR REPLACE FUNCTION fn_validate_journal_min_lines() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_count INT;
    v_total NUMERIC(18,4);
BEGIN
    SELECT COUNT(*), COALESCE(SUM(debit + credit), 0)
      INTO v_count, v_total
      FROM journal_lines
     WHERE journal_entry_id = NEW.journal_entry_id;
    IF v_count < 2 THEN
        RAISE EXCEPTION 'journal entry % has < 2 lines (got %)', NEW.journal_entry_id, v_count;
    END IF;
    IF v_total = 0 THEN
        RAISE EXCEPTION 'journal entry % has zero total movement', NEW.journal_entry_id;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_validate_journal_min_lines ON journal_lines;
CREATE CONSTRAINT TRIGGER trg_validate_journal_min_lines
AFTER INSERT ON journal_lines
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION fn_validate_journal_min_lines();

COMMIT;
