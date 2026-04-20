-- ============================================================================
-- Migration 019: Accounting Hardening — Immutable Ledger, Period Closing
--
-- SAFE: All operations are ADDITIVE only.
--   - Adds IMMUTABILITY triggers (prevent UPDATE/DELETE on journal tables)
--   - Adds accounting_periods table for month/year close
--   - Adds source_event column for audit trail
--   - Adds period management permissions
--   - Does NOT modify or remove any existing data
-- ============================================================================
BEGIN;

-- ════════════════════════════════════════════════════════════════════════════
-- 1. IMMUTABLE LEDGER — DB-level protection
--    NO UPDATE / NO DELETE on journal_entries and journal_lines.
--    Only exception: marking an entry as reversed (is_reversed, reversed_by).
-- ════════════════════════════════════════════════════════════════════════════

-- Journal Entries: block DELETE entirely, allow UPDATE only for reversal fields
CREATE OR REPLACE FUNCTION fn_protect_journal_entries()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Cannot delete journal entries. Use reversal instead. Entry ID: %', OLD.id;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        -- Only allow updating reversal-related fields and source_event
        IF OLD.restaurant_id    IS DISTINCT FROM NEW.restaurant_id
        OR OLD.branch_id        IS DISTINCT FROM NEW.branch_id
        OR OLD.entry_date       IS DISTINCT FROM NEW.entry_date
        OR OLD.reference_type   IS DISTINCT FROM NEW.reference_type
        OR OLD.reference_id     IS DISTINCT FROM NEW.reference_id
        OR OLD.description      IS DISTINCT FROM NEW.description
        OR OLD.created_by       IS DISTINCT FROM NEW.created_by
        OR OLD.created_at       IS DISTINCT FROM NEW.created_at
        THEN
            RAISE EXCEPTION 'Journal entries are immutable. Only reversal fields and source_event can be modified. Entry ID: %', OLD.id;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_protect_journal_entries ON journal_entries;
CREATE TRIGGER trg_protect_journal_entries
    BEFORE UPDATE OR DELETE ON journal_entries
    FOR EACH ROW
    EXECUTE FUNCTION fn_protect_journal_entries();


-- Journal Lines: block ALL updates and deletes
CREATE OR REPLACE FUNCTION fn_protect_journal_lines()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Cannot delete journal lines. Reverse the parent journal entry instead. Line ID: %', OLD.id;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'Journal lines are immutable. Reverse the parent journal entry instead. Line ID: %', OLD.id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_protect_journal_lines ON journal_lines;
CREATE TRIGGER trg_protect_journal_lines
    BEFORE UPDATE OR DELETE ON journal_lines
    FOR EACH ROW
    EXECUTE FUNCTION fn_protect_journal_lines();


-- ════════════════════════════════════════════════════════════════════════════
-- 2. ACCOUNTING PERIODS — Month/year close with entry locking
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS accounting_periods (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    period_start    DATE NOT NULL,
    period_end      DATE NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'closed', 'locked')),
    closed_by       TEXT,
    closed_at       TIMESTAMPTZ,
    reopened_by     TEXT,
    reopened_at     TIMESTAMPTZ,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(restaurant_id, period_start, period_end)
);

CREATE INDEX IF NOT EXISTS idx_ap_restaurant_status
    ON accounting_periods(restaurant_id, status);

CREATE INDEX IF NOT EXISTS idx_ap_restaurant_dates
    ON accounting_periods(restaurant_id, period_start, period_end);


-- Trigger: prevent journal entries in closed periods
CREATE OR REPLACE FUNCTION fn_check_journal_period()
RETURNS TRIGGER AS $$
DECLARE
    v_period RECORD;
BEGIN
    -- Check if the entry date falls within any closed/locked period
    SELECT id, status INTO v_period
    FROM accounting_periods
    WHERE restaurant_id = NEW.restaurant_id
      AND NEW.entry_date BETWEEN period_start AND period_end
      AND status IN ('closed', 'locked')
    LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION 'Cannot create journal entry in % period (% to %). Entry date: %',
            v_period.status,
            (SELECT period_start FROM accounting_periods WHERE id = v_period.id),
            (SELECT period_end FROM accounting_periods WHERE id = v_period.id),
            NEW.entry_date;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_check_journal_period ON journal_entries;
CREATE TRIGGER trg_check_journal_period
    BEFORE INSERT ON journal_entries
    FOR EACH ROW
    EXECUTE FUNCTION fn_check_journal_period();


-- ════════════════════════════════════════════════════════════════════════════
-- 3. AUDIT TRAIL — source_event on journal_entries
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE journal_entries
    ADD COLUMN IF NOT EXISTS source_event VARCHAR(100);

-- NOTE: The immutability trigger above only allows reversal-field updates.
-- source_event is set at INSERT time by the accounting engine.


-- ════════════════════════════════════════════════════════════════════════════
-- 4. PERMISSIONS
-- ════════════════════════════════════════════════════════════════════════════

INSERT INTO permissions (key) VALUES
    ('accounting.close_period'),
    ('accounting.reopen_period')
ON CONFLICT (key) DO NOTHING;

-- Grant to owner only (financial control)
WITH role_perm(role_name, perm_key, allowed, meta) AS (
    VALUES
    ('owner', 'accounting.close_period',  true, '{}'::jsonb),
    ('owner', 'accounting.reopen_period', true, '{}'::jsonb)
)
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, rp.allowed, rp.meta
FROM role_perm rp
JOIN permissions p ON p.key = rp.perm_key
JOIN roles r ON lower(r.name) = lower(rp.role_name)
ON CONFLICT (role_id, permission_id) DO NOTHING;


-- ════════════════════════════════════════════════════════════════════════════
-- 5. REFERENCE TYPE: shift_close for cash drawer accounting
-- ════════════════════════════════════════════════════════════════════════════

-- No DDL needed — reference_type is VARCHAR, not enum.
-- Just noting the new valid types: 'shift_close', 'period_close'

COMMIT;
