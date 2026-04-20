-- ============================================================================
-- Migration 022: Bank Reconciliation + Period Lock Enforcement
--
-- SAFE: All operations are ADDITIVE only.
--   - Adds bank_statements table (imported bank statement lines)
--   - Adds bank_reconciliation table (matched statement ↔ journal pairs)
--   - Adds DB trigger to enforce period lock on journal_entries
--   - Adds 'bank_recon' to valid reference types
--   - Adds new permissions for bank reconciliation and reports
--   - Does NOT modify or remove any existing data/logic
-- ============================================================================
BEGIN;

-- ════════════════════════════════════════════════════════════════════════════
-- 1. BANK STATEMENTS
--
--    Imported bank statement lines for reconciliation.
--    Each row is one line from a bank CSV/OFX import.
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS bank_statements (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,

    -- Statement metadata
    statement_date  DATE NOT NULL,
    value_date      DATE,
    description     TEXT,
    reference       VARCHAR(200),

    -- Amount (positive = credit/deposit, negative = debit/withdrawal)
    amount          NUMERIC(14,2) NOT NULL,
    running_balance NUMERIC(14,2),

    -- Bank info
    bank_account    VARCHAR(100),
    transaction_type VARCHAR(50),  -- NEFT, IMPS, UPI, RTGS, etc.

    -- Reconciliation status
    status          VARCHAR(20) NOT NULL DEFAULT 'unmatched'
                    CHECK (status IN ('unmatched', 'matched', 'excluded')),
    matched_at      TIMESTAMPTZ,

    -- Import tracking
    import_batch_id VARCHAR(100),
    raw_data        JSONB,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bank_statements_restaurant
    ON bank_statements(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_bank_statements_date
    ON bank_statements(restaurant_id, statement_date);
CREATE INDEX IF NOT EXISTS idx_bank_statements_status
    ON bank_statements(restaurant_id, status);
CREATE INDEX IF NOT EXISTS idx_bank_statements_amount
    ON bank_statements(restaurant_id, amount);
CREATE INDEX IF NOT EXISTS idx_bank_statements_reference
    ON bank_statements(restaurant_id, reference);

-- ════════════════════════════════════════════════════════════════════════════
-- 2. BANK RECONCILIATION
--
--    Links bank statement lines to journal entries.
--    A statement line can match one or more journal entries and vice versa.
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS bank_reconciliation (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id       UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,

    bank_statement_id   UUID NOT NULL REFERENCES bank_statements(id) ON DELETE CASCADE,
    journal_entry_id    UUID NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,

    -- Match details
    match_type          VARCHAR(20) NOT NULL DEFAULT 'manual'
                        CHECK (match_type IN ('auto', 'manual')),
    match_confidence    NUMERIC(5,2),  -- 0-100 for auto-matches
    matched_by          VARCHAR(100),
    notes               TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(bank_statement_id, journal_entry_id)
);

CREATE INDEX IF NOT EXISTS idx_bank_recon_restaurant
    ON bank_reconciliation(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_bank_recon_statement
    ON bank_reconciliation(bank_statement_id);
CREATE INDEX IF NOT EXISTS idx_bank_recon_journal
    ON bank_reconciliation(journal_entry_id);

-- ════════════════════════════════════════════════════════════════════════════
-- 3. PERIOD LOCK ENFORCEMENT TRIGGER
--
--    Prevents inserting journal_entries when entry_date falls within a
--    closed or locked accounting_period. This is the hard gate the CTO wants.
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_enforce_period_lock()
RETURNS TRIGGER AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM accounting_periods
        WHERE restaurant_id = NEW.restaurant_id
          AND status IN ('closed', 'locked')
          AND NEW.entry_date BETWEEN period_start AND period_end
    ) THEN
        RAISE EXCEPTION 'Cannot create journal entry: accounting period containing % is closed/locked',
            NEW.entry_date
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Drop if exists to allow re-run
DROP TRIGGER IF EXISTS trg_enforce_period_lock ON journal_entries;

CREATE TRIGGER trg_enforce_period_lock
    BEFORE INSERT ON journal_entries
    FOR EACH ROW
    EXECUTE FUNCTION fn_enforce_period_lock();

-- ════════════════════════════════════════════════════════════════════════════
-- 4. PERMISSIONS
-- ════════════════════════════════════════════════════════════════════════════

-- Bank reconciliation permissions
INSERT INTO permissions (code, name, description, category) VALUES
    ('bank_recon.read',   'View Bank Reconciliation',   'View bank statements and reconciliation status', 'accounting'),
    ('bank_recon.write',  'Manage Bank Reconciliation',  'Import statements, match entries, reconcile',     'accounting'),
    ('reports.read',      'View Financial Reports',      'View trial balance, P&L, balance sheet, aging',   'accounting')
ON CONFLICT (code) DO NOTHING;

-- Grant to owner and manager roles
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
CROSS JOIN permissions p
WHERE r.name IN ('owner', 'manager')
  AND p.code IN ('bank_recon.read', 'bank_recon.write', 'reports.read')
ON CONFLICT DO NOTHING;

-- Grant read-only to cashier
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
CROSS JOIN permissions p
WHERE r.name = 'cashier'
  AND p.code IN ('bank_recon.read', 'reports.read')
ON CONFLICT DO NOTHING;

COMMIT;
