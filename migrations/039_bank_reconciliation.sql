-- ════════════════════════════════════════════════════════════════════════════
-- Migration 039 — Bank Reconciliation Engine (Phase 3 of fintech recon core)
--
-- Builds a bank-statement-driven reconciliation pipeline that sits on top of
-- Phase 1 (merchant_ledger) and Phase 2 (escrow_ledger).
--
--     bank_recon_accounts      one row per merchant bank account
--     bank_recon_imports       one row per CSV / webhook batch
--     bank_recon_lines         one row per bank statement line (idempotent)
--     bank_recon_runs          one row per match-engine execution
--     bank_recon_discrepancies one row per detected mismatch
--     platform_admin_users     direct membership for cross-merchant access
--
-- All tables are isolated under the `bank_recon_*` namespace so they do NOT
-- collide with the legacy `bank_statements` / `reconciliation_*` tables that
-- earlier ad-hoc work created.
-- ════════════════════════════════════════════════════════════════════════════

BEGIN;

-- ── Enums ─────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'bank_recon_import_source') THEN
        CREATE TYPE bank_recon_import_source AS ENUM (
            'csv_upload', 'webhook', 'manual'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'bank_recon_import_status') THEN
        CREATE TYPE bank_recon_import_status AS ENUM (
            'pending', 'processing', 'completed', 'failed'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'bank_recon_line_status') THEN
        CREATE TYPE bank_recon_line_status AS ENUM (
            'unmatched', 'matched', 'partial', 'ignored'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'bank_recon_run_status') THEN
        CREATE TYPE bank_recon_run_status AS ENUM (
            'running', 'completed', 'failed'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'bank_recon_discrepancy_kind') THEN
        CREATE TYPE bank_recon_discrepancy_kind AS ENUM (
            'missing_in_bank',         -- settlement settled, no bank line
            'missing_in_settlement',   -- bank credit, no settlement
            'amount_mismatch',         -- matched but amount differs
            'date_mismatch',           -- matched but date out of window
            'duplicate_bank_line',     -- two bank lines, same UTR
            'orphan_credit',           -- bank credit, no upstream record
            'orphan_debit'             -- bank debit, unexplained
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'bank_recon_discrepancy_status') THEN
        CREATE TYPE bank_recon_discrepancy_status AS ENUM (
            'open', 'investigating', 'resolved', 'ignored'
        );
    END IF;
END$$;

-- ── 1. Platform admin membership ──────────────────────────────────────────
-- Direct user_id list (no role coupling). A user listed here may invoke the
-- /admin/bank-recon/* endpoints and see data for ALL merchants. Merchants
-- (owners/managers) NEVER appear here unless explicitly added.
CREATE TABLE IF NOT EXISTS platform_admin_users (
    user_id    UUID        PRIMARY KEY,
    email      TEXT,
    notes      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by UUID
);

-- ── 2. Bank account registry ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bank_recon_accounts (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id            UUID NOT NULL,
    account_label          TEXT NOT NULL,
    bank_name              TEXT,
    account_number_last4   VARCHAR(4),
    ifsc                   VARCHAR(16),
    currency               CHAR(3) NOT NULL DEFAULT 'INR',
    is_active              BOOLEAN NOT NULL DEFAULT true,
    metadata               JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (merchant_id, account_label)
);

CREATE INDEX IF NOT EXISTS idx_bank_recon_accounts_merchant
    ON bank_recon_accounts (merchant_id) WHERE is_active = true;

-- ── 3. Import batches ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bank_recon_imports (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id         UUID NOT NULL,
    account_id          UUID NOT NULL REFERENCES bank_recon_accounts(id) ON DELETE CASCADE,
    source              bank_recon_import_source NOT NULL,
    original_filename   TEXT,
    row_count           INTEGER NOT NULL DEFAULT 0,
    rows_inserted       INTEGER NOT NULL DEFAULT 0,
    rows_skipped        INTEGER NOT NULL DEFAULT 0,
    status              bank_recon_import_status NOT NULL DEFAULT 'pending',
    error_message       TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ,
    imported_by         UUID
);

CREATE INDEX IF NOT EXISTS idx_bank_recon_imports_merchant_started
    ON bank_recon_imports (merchant_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_bank_recon_imports_status
    ON bank_recon_imports (status, started_at DESC);

-- ── 4. Bank statement lines ───────────────────────────────────────────────
-- `amount` is signed: positive = credit (money in), negative = debit (money out).
-- `line_hash` is a sha256 over canonical fields used for idempotent re-upload.
CREATE TABLE IF NOT EXISTS bank_recon_lines (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    import_id                UUID NOT NULL REFERENCES bank_recon_imports(id) ON DELETE CASCADE,
    merchant_id              UUID NOT NULL,
    account_id               UUID NOT NULL REFERENCES bank_recon_accounts(id) ON DELETE CASCADE,
    posted_date              DATE NOT NULL,
    value_date               DATE,
    amount                   NUMERIC(18,4) NOT NULL,
    currency                 CHAR(3) NOT NULL DEFAULT 'INR',
    narration                TEXT,
    bank_reference           TEXT,                       -- UTR / RRN / txn id
    counterparty             TEXT,
    balance_after            NUMERIC(18,4),
    line_hash                TEXT NOT NULL,
    match_status             bank_recon_line_status NOT NULL DEFAULT 'unmatched',
    matched_settlement_id    UUID,
    matched_escrow_entry_id  UUID,
    match_confidence         NUMERIC(5,4),               -- 0.0000-1.0000
    matched_at               TIMESTAMPTZ,
    matched_by               TEXT,                       -- 'auto' | user_id | 'manual'
    raw_row                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (merchant_id, account_id, line_hash),
    CHECK (amount <> 0)
);

CREATE INDEX IF NOT EXISTS idx_bank_recon_lines_merchant_date
    ON bank_recon_lines (merchant_id, posted_date DESC);
CREATE INDEX IF NOT EXISTS idx_bank_recon_lines_account_date
    ON bank_recon_lines (account_id, posted_date DESC);
CREATE INDEX IF NOT EXISTS idx_bank_recon_lines_status
    ON bank_recon_lines (merchant_id, match_status, posted_date DESC);
CREATE INDEX IF NOT EXISTS idx_bank_recon_lines_bank_reference
    ON bank_recon_lines (bank_reference) WHERE bank_reference IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bank_recon_lines_settlement
    ON bank_recon_lines (matched_settlement_id) WHERE matched_settlement_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bank_recon_lines_escrow
    ON bank_recon_lines (matched_escrow_entry_id) WHERE matched_escrow_entry_id IS NOT NULL;

-- ── 5. Match-engine runs ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bank_recon_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id     UUID,                                -- NULL = global / all merchants
    account_id      UUID REFERENCES bank_recon_accounts(id) ON DELETE SET NULL,
    scope_from      DATE,
    scope_to        DATE,
    triggered_by    UUID,
    is_admin_run    BOOLEAN NOT NULL DEFAULT false,
    status          bank_recon_run_status NOT NULL DEFAULT 'running',
    summary         JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message   TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_bank_recon_runs_merchant_started
    ON bank_recon_runs (merchant_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_bank_recon_runs_global
    ON bank_recon_runs (started_at DESC) WHERE merchant_id IS NULL;

-- ── 6. Discrepancies ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bank_recon_discrepancies (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id               UUID REFERENCES bank_recon_runs(id) ON DELETE SET NULL,
    merchant_id          UUID NOT NULL,
    account_id           UUID,
    kind                 bank_recon_discrepancy_kind NOT NULL,
    severity             VARCHAR(16) NOT NULL DEFAULT 'medium',  -- low|medium|high|critical
    line_id              UUID REFERENCES bank_recon_lines(id) ON DELETE SET NULL,
    settlement_id        UUID,
    escrow_entry_id      UUID,
    expected_amount      NUMERIC(18,4),
    actual_amount        NUMERIC(18,4),
    variance_amount      NUMERIC(18,4),
    notes                TEXT,
    status               bank_recon_discrepancy_status NOT NULL DEFAULT 'open',
    resolution_notes     TEXT,
    resolved_at          TIMESTAMPTZ,
    resolved_by          UUID,
    metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
    detected_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (merchant_id, kind, line_id, settlement_id, escrow_entry_id)
);

CREATE INDEX IF NOT EXISTS idx_bank_recon_disc_merchant_status
    ON bank_recon_discrepancies (merchant_id, status, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_bank_recon_disc_kind
    ON bank_recon_discrepancies (kind, status);
CREATE INDEX IF NOT EXISTS idx_bank_recon_disc_run
    ON bank_recon_discrepancies (run_id);

-- ── 7. Helper view: open discrepancy counts per merchant ─────────────────
CREATE OR REPLACE VIEW v_bank_recon_open_discrepancies AS
SELECT merchant_id,
       kind,
       severity,
       count(*) AS cnt,
       sum(coalesce(variance_amount, 0)) AS total_variance
  FROM bank_recon_discrepancies
 WHERE status IN ('open', 'investigating')
 GROUP BY merchant_id, kind, severity;

-- ── 8. RBAC permissions (merchant-scoped) ────────────────────────────────
INSERT INTO permissions (key) VALUES
    ('recon.read'),
    ('recon.write'),
    ('recon.admin')
ON CONFLICT (key) DO NOTHING;

-- Owner: all 3. Manager: read + write. Cashier/Waiter/Staff: read.
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
  FROM roles r
  JOIN permissions p ON p.key IN ('recon.read', 'recon.write', 'recon.admin')
 WHERE r.name = 'owner'
ON CONFLICT (role_id, permission_id) DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
  FROM roles r
  JOIN permissions p ON p.key IN ('recon.read', 'recon.write')
 WHERE r.name = 'manager'
ON CONFLICT (role_id, permission_id) DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
  FROM roles r
  JOIN permissions p ON p.key = 'recon.read'
 WHERE r.name IN ('cashier', 'waiter', 'staff')
ON CONFLICT (role_id, permission_id) DO NOTHING;

-- ── 9. Helper: is_platform_admin(user_id) ────────────────────────────────
CREATE OR REPLACE FUNCTION fn_is_platform_admin(p_user_id UUID)
RETURNS BOOLEAN AS $$
    SELECT EXISTS (SELECT 1 FROM platform_admin_users WHERE user_id = p_user_id);
$$ LANGUAGE sql STABLE;

COMMIT;
