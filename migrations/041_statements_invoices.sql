-- ════════════════════════════════════════════════════════════════════════
-- Migration 041 — Statements & Tax Invoices (Phase 5)
-- ════════════════════════════════════════════════════════════════════════
--
-- Adds:
--   • tax_invoices            — Bittu-issued tax invoices to the merchant
--   • tax_invoice_line_items  — line items per invoice
--   • tax_invoice_seq         — per-(merchant, FY) numbering
--   • merchant_statements     — cached settlement statements over a period
--   • Enums: invoice_status, statement_status
--   • SQL fns:
--       fn_next_invoice_number(merchant_id, invoice_date)
--           → 'INV-{FY}-{MERCHANT4}-{NNNNN}' e.g. INV-2526-A1B2-00001
--       fn_compute_statement(merchant_id, period_start, period_end, currency)
--           → table of (opening_balance, total_credits, total_debits,
--                       closing_balance, txn_count)
--   • Permissions: invoice.read, invoice.write, invoice.admin,
--                  statement.read, statement.generate
--
-- HARD RULE: NO third-party invoicing/payment gateway integration. Pure
-- internal numbering, computed totals from the merchant_ledger.
-- ════════════════════════════════════════════════════════════════════════

-- ── 1. Enums ────────────────────────────────────────────────────────────
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'invoice_status') THEN
        CREATE TYPE invoice_status AS ENUM (
            'draft', 'issued', 'cancelled'
        );
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'statement_status') THEN
        CREATE TYPE statement_status AS ENUM (
            'generating', 'ready', 'cancelled'
        );
    END IF;
END $$;


-- ── 2. tax_invoices ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tax_invoices (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_number      TEXT NOT NULL,
    merchant_id         UUID NOT NULL,
    branch_id           UUID,

    invoice_date        DATE NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::date,
    period_start        DATE,
    period_end          DATE,
    due_date            DATE,

    currency            CHAR(3) NOT NULL DEFAULT 'INR',

    -- Computed from line items via recompute_totals()
    subtotal            NUMERIC(18,4) NOT NULL DEFAULT 0,
    cgst_total          NUMERIC(18,4) NOT NULL DEFAULT 0,
    sgst_total          NUMERIC(18,4) NOT NULL DEFAULT 0,
    igst_total          NUMERIC(18,4) NOT NULL DEFAULT 0,
    cess_total          NUMERIC(18,4) NOT NULL DEFAULT 0,
    discount_total      NUMERIC(18,4) NOT NULL DEFAULT 0,
    total_amount        NUMERIC(18,4) NOT NULL DEFAULT 0,

    -- GST envelope
    place_of_supply     TEXT,
    gstin_supplier      TEXT,        -- Bittu's GSTIN
    gstin_customer      TEXT,        -- Merchant's GSTIN
    supplier_name       TEXT,
    supplier_address    TEXT,
    customer_name       TEXT,
    customer_address    TEXT,

    notes               TEXT,
    status              invoice_status NOT NULL DEFAULT 'draft',

    file_path           TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by          UUID,
    issued_at           TIMESTAMPTZ,
    issued_by           UUID,
    cancelled_at        TIMESTAMPTZ,
    cancelled_by        UUID,
    cancellation_reason TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_tax_invoices_number UNIQUE (invoice_number)
);

CREATE INDEX IF NOT EXISTS idx_tax_invoices_merchant
    ON tax_invoices (merchant_id, invoice_date DESC);
CREATE INDEX IF NOT EXISTS idx_tax_invoices_status
    ON tax_invoices (status);


-- ── 3. tax_invoice_line_items ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tax_invoice_line_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id          UUID NOT NULL REFERENCES tax_invoices(id) ON DELETE CASCADE,
    sno                 INTEGER NOT NULL,

    description         TEXT NOT NULL,
    hsn_sac             TEXT,           -- HSN code or SAC
    quantity            NUMERIC(18,4) NOT NULL DEFAULT 1,
    unit_amount         NUMERIC(18,4) NOT NULL DEFAULT 0,
    discount_amount     NUMERIC(18,4) NOT NULL DEFAULT 0,
    taxable_amount      NUMERIC(18,4) NOT NULL DEFAULT 0,

    cgst_rate           NUMERIC(5,2) NOT NULL DEFAULT 0,
    cgst_amount         NUMERIC(18,4) NOT NULL DEFAULT 0,
    sgst_rate           NUMERIC(5,2) NOT NULL DEFAULT 0,
    sgst_amount         NUMERIC(18,4) NOT NULL DEFAULT 0,
    igst_rate           NUMERIC(5,2) NOT NULL DEFAULT 0,
    igst_amount         NUMERIC(18,4) NOT NULL DEFAULT 0,
    cess_rate           NUMERIC(5,2) NOT NULL DEFAULT 0,
    cess_amount         NUMERIC(18,4) NOT NULL DEFAULT 0,

    line_total          NUMERIC(18,4) NOT NULL DEFAULT 0,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_tax_invoice_line_sno UNIQUE (invoice_id, sno),
    CONSTRAINT chk_line_quantity_positive CHECK (quantity > 0),
    CONSTRAINT chk_line_amounts_nonneg CHECK (
        unit_amount >= 0 AND discount_amount >= 0 AND taxable_amount >= 0
        AND cgst_amount >= 0 AND sgst_amount >= 0 AND igst_amount >= 0
    )
);

CREATE INDEX IF NOT EXISTS idx_tax_invoice_lines_invoice
    ON tax_invoice_line_items (invoice_id, sno);


-- ── 4. tax_invoice_seq (per-merchant per-FY numbering) ──────────────────
CREATE TABLE IF NOT EXISTS tax_invoice_seq (
    merchant_id UUID NOT NULL,
    fy_code     CHAR(4) NOT NULL,        -- '2526' for FY 2025-26
    last_seq    BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (merchant_id, fy_code)
);


-- ── 5. merchant_statements ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS merchant_statements (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id         UUID NOT NULL,
    period_start        TIMESTAMPTZ NOT NULL,
    period_end          TIMESTAMPTZ NOT NULL,
    currency            CHAR(3) NOT NULL DEFAULT 'INR',

    opening_balance     NUMERIC(18,4) NOT NULL DEFAULT 0,
    total_credits       NUMERIC(18,4) NOT NULL DEFAULT 0,
    total_debits        NUMERIC(18,4) NOT NULL DEFAULT 0,
    closing_balance     NUMERIC(18,4) NOT NULL DEFAULT 0,
    txn_count           INTEGER NOT NULL DEFAULT 0,

    breakdown           JSONB NOT NULL DEFAULT '{}'::jsonb,   -- per-txn-type sums
    status              statement_status NOT NULL DEFAULT 'generating',
    file_format         TEXT,                                  -- 'csv'|'json'
    file_path           TEXT,
    notes               TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,

    generated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    generated_by        UUID,

    CONSTRAINT chk_statement_period CHECK (period_end > period_start)
);

-- Cancellation columns (added idempotently so re-running is safe)
ALTER TABLE merchant_statements
    ADD COLUMN IF NOT EXISTS cancelled_at         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cancelled_by         UUID,
    ADD COLUMN IF NOT EXISTS cancellation_reason  TEXT;

CREATE INDEX IF NOT EXISTS idx_merchant_statements_merchant
    ON merchant_statements (merchant_id, period_end DESC);


-- ── 6. Triggers ─────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_tax_invoice_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tax_invoice_touch ON tax_invoices;
CREATE TRIGGER trg_tax_invoice_touch
    BEFORE UPDATE ON tax_invoices
    FOR EACH ROW
    EXECUTE FUNCTION fn_tax_invoice_touch_updated_at();


-- ── 7. Helper functions ─────────────────────────────────────────────────

-- Compute Indian financial year code (e.g. 2025-04-01..2026-03-31 → '2526')
CREATE OR REPLACE FUNCTION fn_indian_fy_code(p_date DATE)
RETURNS CHAR(4) AS $$
DECLARE
    yr INT := EXTRACT(YEAR FROM p_date)::INT;
    mo INT := EXTRACT(MONTH FROM p_date)::INT;
    fy_start INT;
BEGIN
    IF mo >= 4 THEN
        fy_start := yr;
    ELSE
        fy_start := yr - 1;
    END IF;
    RETURN lpad((fy_start % 100)::text, 2, '0')
         || lpad(((fy_start + 1) % 100)::text, 2, '0');
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- Allocate the next invoice number for a (merchant, FY).
CREATE OR REPLACE FUNCTION fn_next_invoice_number(
    p_merchant_id UUID,
    p_invoice_date DATE
) RETURNS TEXT AS $$
DECLARE
    v_fy   CHAR(4) := fn_indian_fy_code(p_invoice_date);
    v_seq  BIGINT;
    v_m4   TEXT := upper(substring(replace(p_merchant_id::text, '-', ''), 1, 4));
BEGIN
    INSERT INTO tax_invoice_seq (merchant_id, fy_code, last_seq)
    VALUES (p_merchant_id, v_fy, 1)
    ON CONFLICT (merchant_id, fy_code) DO UPDATE
       SET last_seq = tax_invoice_seq.last_seq + 1
    RETURNING last_seq INTO v_seq;
    RETURN format('INV-%s-%s-%s', v_fy, v_m4, lpad(v_seq::text, 5, '0'));
END;
$$ LANGUAGE plpgsql;

-- Compute the statement aggregates for a merchant over a period.
-- Returns one row.
CREATE OR REPLACE FUNCTION fn_compute_statement(
    p_merchant_id UUID,
    p_period_start TIMESTAMPTZ,
    p_period_end   TIMESTAMPTZ,
    p_currency     CHAR(3) DEFAULT 'INR'
) RETURNS TABLE (
    opening_balance NUMERIC,
    total_credits   NUMERIC,
    total_debits    NUMERIC,
    closing_balance NUMERIC,
    txn_count       BIGINT,
    breakdown       JSONB
) AS $$
DECLARE
    v_open  NUMERIC := 0;
    v_close NUMERIC := 0;
    v_cred  NUMERIC := 0;
    v_deb   NUMERIC := 0;
    v_cnt   BIGINT  := 0;
    v_bd    JSONB;
BEGIN
    -- Opening balance: balance_after of last entry strictly BEFORE period_start
    SELECT COALESCE(balance_after, 0) INTO v_open
      FROM merchant_ledger
     WHERE merchant_id = p_merchant_id
       AND currency = upper(p_currency)
       AND created_at < p_period_start
     ORDER BY created_at DESC, ledger_reference DESC
     LIMIT 1;

    -- Aggregate over the period
    SELECT COALESCE(SUM(credit_amount), 0),
           COALESCE(SUM(debit_amount), 0),
           COUNT(*)
      INTO v_cred, v_deb, v_cnt
      FROM merchant_ledger
     WHERE merchant_id = p_merchant_id
       AND currency = upper(p_currency)
       AND created_at >= p_period_start
       AND created_at <  p_period_end;

    -- Closing balance: last entry within (or before end of) period — fall
    -- back to opening if no activity.
    SELECT COALESCE(balance_after, v_open) INTO v_close
      FROM merchant_ledger
     WHERE merchant_id = p_merchant_id
       AND currency = upper(p_currency)
       AND created_at < p_period_end
     ORDER BY created_at DESC, ledger_reference DESC
     LIMIT 1;

    -- Per-txn-type breakdown
    SELECT COALESCE(jsonb_object_agg(transaction_type::text, sums), '{}'::jsonb)
      INTO v_bd
      FROM (
        SELECT transaction_type,
               jsonb_build_object(
                   'credit', SUM(credit_amount),
                   'debit',  SUM(debit_amount),
                   'count',  COUNT(*)
               ) AS sums
          FROM merchant_ledger
         WHERE merchant_id = p_merchant_id
           AND currency = upper(p_currency)
           AND created_at >= p_period_start
           AND created_at <  p_period_end
         GROUP BY transaction_type
      ) t;

    opening_balance := v_open;
    total_credits   := v_cred;
    total_debits    := v_deb;
    closing_balance := v_close;
    txn_count       := v_cnt;
    breakdown       := v_bd;
    RETURN NEXT;
END;
$$ LANGUAGE plpgsql STABLE;


-- ── 8. RBAC permissions ─────────────────────────────────────────────────
INSERT INTO permissions (key) VALUES
    ('invoice.read'),
    ('invoice.write'),
    ('invoice.admin'),
    ('statement.read'),
    ('statement.generate')
ON CONFLICT (key) DO NOTHING;

-- Owner: read/write invoices, read/generate statements
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
  FROM roles r
  JOIN permissions p ON p.key IN
       ('invoice.read', 'invoice.write', 'statement.read', 'statement.generate')
 WHERE r.name = 'owner'
ON CONFLICT (role_id, permission_id) DO NOTHING;

-- Manager: same minus invoice.write (cannot draft/issue invoices)
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
  FROM roles r
  JOIN permissions p ON p.key IN
       ('invoice.read', 'statement.read', 'statement.generate')
 WHERE r.name = 'manager'
ON CONFLICT (role_id, permission_id) DO NOTHING;

-- Cashier/staff/waiter: read only
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
  FROM roles r
  JOIN permissions p ON p.key IN ('invoice.read', 'statement.read')
 WHERE r.name IN ('cashier', 'waiter', 'staff')
ON CONFLICT (role_id, permission_id) DO NOTHING;

COMMIT;
