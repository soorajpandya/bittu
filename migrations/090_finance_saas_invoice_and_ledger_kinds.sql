-- =====================================================================
-- Migration 090 — Finance consistency: Bittu→merchant SaaS tax invoice
--                 sourced from the Route mirror + entitlement-ledger kinds
-- =====================================================================
-- D1: Bittu's taxable supply is the full 5% deduction (4.2373% fee + 18% GST).
-- D3: escrow is retired under Route (no schema change; see scripts/escrow_zero_out.py).
--
-- * bittu_saas_invoices gains the basis, refund credits, unresolved count,
--   a frozen flag and a net payable; status now also allows 'credit_noted'.
-- * bittu_saas_credit_notes: one row per late refund against an already
--   issued month (never rewrite an issued invoice).
-- * merchant_ledger_txn_type gains 'fee_reversal' / 'gst_reversal' so the
--   fee + GST Bittu no longer earns on a refunded/charged-back portion can be
--   credited back and the ledger nets to the merchant's proportional share.
-- Idempotent: safe to re-run.
-- =====================================================================
BEGIN;

ALTER TABLE bittu_saas_invoices
    ADD COLUMN IF NOT EXISTS basis               TEXT          NOT NULL DEFAULT 'full_consideration',
    ADD COLUMN IF NOT EXISTS online_gross        NUMERIC(14,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_deduction     NUMERIC(14,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS refund_credit_fee   NUMERIC(14,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS refund_credit_gst   NUMERIC(14,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS refund_count        INTEGER       NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS unresolved_count    INTEGER       NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS net_payable         NUMERIC(14,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS frozen_at           TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS source              TEXT          NOT NULL DEFAULT 'razorpay_route_mirror';

-- Pre-existing rows were computed from merchant_ledger fee legs that were
-- never written; mark them so they are recomputed on next render.
UPDATE bittu_saas_invoices
   SET status = 'draft', source = 'legacy_merchant_ledger'
 WHERE source = 'razorpay_route_mirror' AND frozen_at IS NULL AND total_payable = 0;

ALTER TABLE bittu_saas_invoices DROP CONSTRAINT IF EXISTS bittu_saas_invoices_status_check;
ALTER TABLE bittu_saas_invoices
    ADD CONSTRAINT bittu_saas_invoices_status_check
    CHECK (status IN ('draft', 'issued', 'credit_noted', 'paid', 'void'));

CREATE TABLE IF NOT EXISTS bittu_saas_credit_notes (
    id                  BIGSERIAL PRIMARY KEY,
    credit_note_number  TEXT NOT NULL,
    invoice_id          BIGINT NOT NULL REFERENCES bittu_saas_invoices(id) ON DELETE RESTRICT,
    merchant_id         UUID NOT NULL,
    refund_id           TEXT NOT NULL,                 -- rzp refund id (or dispute ref)
    razorpay_payment_id TEXT,
    currency            CHAR(3) NOT NULL DEFAULT 'INR',
    recovered_amount    NUMERIC(14,2) NOT NULL DEFAULT 0,  -- gross refunded to customer
    fee_credit          NUMERIC(14,2) NOT NULL DEFAULT 0,
    gst_credit          NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_credit        NUMERIC(14,2) NOT NULL DEFAULT 0,
    reason              TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_bittu_saas_credit_notes_refund UNIQUE (refund_id),
    CONSTRAINT uq_bittu_saas_credit_notes_number UNIQUE (credit_note_number)
);
CREATE INDEX IF NOT EXISTS ix_bittu_saas_credit_notes_invoice ON bittu_saas_credit_notes (invoice_id);

COMMENT ON TABLE bittu_saas_credit_notes IS
    'Credit notes against an issued Bittu SaaS invoice for refunds processed after the month was frozen. Issued invoices are never rewritten.';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_enum WHERE enumtypid = 'merchant_ledger_txn_type'::regtype AND enumlabel = 'fee_reversal') THEN
        ALTER TYPE merchant_ledger_txn_type ADD VALUE 'fee_reversal';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_enum WHERE enumtypid = 'merchant_ledger_txn_type'::regtype AND enumlabel = 'gst_reversal') THEN
        ALTER TYPE merchant_ledger_txn_type ADD VALUE 'gst_reversal';
    END IF;
END$$;

COMMIT;
