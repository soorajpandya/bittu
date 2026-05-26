-- ============================================================================
-- 074_razorpay_kyc_batch.sql
--
-- Razorpay Route Linked-Account onboarding via *manual batch CSV upload* on
-- the Razorpay Dashboard. Razorpay does NOT expose a bulk linked-account
-- creation API, so we collect KYC into Supabase, group merchants into
-- timed batches (every 30 min), generate a CSV matching the Razorpay
-- template, and an admin uploads it on the dashboard.
--
-- Queue-based design — new batches always generate on schedule, individual
-- merchants are locked at row-level via ``batch_id``. A merchant included
-- in one batch never re-appears in another batch unless an admin resets it.
-- ============================================================================

-- ── Status enums ────────────────────────────────────────────────────────────
DO $$ BEGIN
    CREATE TYPE rzp_kyc_submission_status AS ENUM (
        'PENDING_BATCH_UPLOAD',
        'IN_BATCH_FILE',
        'UPLOADED_TO_RAZORPAY',
        'APPROVED',
        'REJECTED'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE rzp_kyc_batch_status AS ENUM (
        'GENERATED',         -- CSV produced, awaiting admin download
        'DOWNLOADED',        -- admin pulled the CSV
        'UPLOADED',          -- admin marked it uploaded to Razorpay Dashboard
        'PARTIALLY_APPROVED',-- mix of approved + rejected merchants
        'APPROVED',          -- all merchants approved
        'REJECTED'           -- batch failed at Razorpay
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;


-- ── KYC submissions queue ───────────────────────────────────────────────────
-- One row per merchant submission. Once a merchant is APPROVED we keep the
-- row for audit but never re-include it in a batch (uniqueness below).
CREATE TABLE IF NOT EXISTS rzp_kyc_submissions (
    id                          BIGSERIAL PRIMARY KEY,
    merchant_id                 UUID NOT NULL,

    -- Razorpay batch-CSV columns (verbatim names) ────────────────────────────
    account_name                TEXT NOT NULL,
    account_email               TEXT NOT NULL,
    dashboard_access            SMALLINT NOT NULL DEFAULT 0,   -- 0/1
    customer_refunds            SMALLINT NOT NULL DEFAULT 0,   -- 0/1
    business_name               TEXT NOT NULL,
    business_type               TEXT NOT NULL,                 -- individual | proprietorship | partnership | ...
    ifsc_code                   TEXT NOT NULL,
    account_number              TEXT NOT NULL,
    beneficiary_name            TEXT NOT NULL,

    -- Lifecycle ───────────────────────────────────────────────────────────────
    status                      rzp_kyc_submission_status NOT NULL DEFAULT 'PENDING_BATCH_UPLOAD',
    batch_id                    BIGINT,                       -- FK below (deferred — batches table created next)
    batch_assigned_at           TIMESTAMPTZ,
    razorpay_account_id         TEXT,                         -- acc_xxx, filled once known
    razorpay_account_status     TEXT,                         -- created | activated | suspended | rejected
    rejection_reason            TEXT,
    notes                       JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at                 TIMESTAMPTZ,
    rejected_at                 TIMESTAMPTZ
);

-- A merchant may only have ONE "live" submission at a time. Rejected ones
-- can be retried (the partial unique index ignores REJECTED).
CREATE UNIQUE INDEX IF NOT EXISTS uq_rzp_kyc_submissions_merchant_active
    ON rzp_kyc_submissions (merchant_id)
    WHERE status <> 'REJECTED';

CREATE INDEX IF NOT EXISTS idx_rzp_kyc_submissions_status
    ON rzp_kyc_submissions (status);
CREATE INDEX IF NOT EXISTS idx_rzp_kyc_submissions_batch_id
    ON rzp_kyc_submissions (batch_id);
CREATE INDEX IF NOT EXISTS idx_rzp_kyc_submissions_merchant_id
    ON rzp_kyc_submissions (merchant_id);
CREATE INDEX IF NOT EXISTS idx_rzp_kyc_submissions_rzp_account_id
    ON rzp_kyc_submissions (razorpay_account_id) WHERE razorpay_account_id IS NOT NULL;


-- ── Batches ─────────────────────────────────────────────────────────────────
-- One row per 30-minute slot. Empty slots also create a row so audit gaps
-- never appear (record_count = 0).
CREATE TABLE IF NOT EXISTS rzp_kyc_batches (
    id                  BIGSERIAL PRIMARY KEY,
    batch_no            TEXT NOT NULL UNIQUE,        -- e.g. BATCH-20260526-1030
    slot_at             TIMESTAMPTZ NOT NULL UNIQUE, -- the 30-min aligned slot timestamp
    record_count        INTEGER NOT NULL DEFAULT 0,
    csv_filename        TEXT,
    csv_bytes           BYTEA,                       -- materialized CSV blob for re-download
    xlsx_bytes          BYTEA,                       -- materialized XLSX blob

    status              rzp_kyc_batch_status NOT NULL DEFAULT 'GENERATED',

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    downloaded_at       TIMESTAMPTZ,
    downloaded_by       UUID,
    uploaded_at         TIMESTAMPTZ,
    uploaded_by         UUID,
    approved_at         TIMESTAMPTZ,
    approved_by         UUID,
    rejected_at         TIMESTAMPTZ,
    rejected_by         UUID,
    notes               JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_rzp_kyc_batches_status
    ON rzp_kyc_batches (status);
CREATE INDEX IF NOT EXISTS idx_rzp_kyc_batches_slot_at
    ON rzp_kyc_batches (slot_at DESC);

-- Now wire the deferred FK from submissions → batches.
DO $$ BEGIN
    ALTER TABLE rzp_kyc_submissions
        ADD CONSTRAINT fk_rzp_kyc_submissions_batch
        FOREIGN KEY (batch_id) REFERENCES rzp_kyc_batches(id)
        ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;


-- ── updated_at trigger ──────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_rzp_kyc_submissions_touch()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_rzp_kyc_submissions_touch ON rzp_kyc_submissions;
CREATE TRIGGER trg_rzp_kyc_submissions_touch
    BEFORE UPDATE ON rzp_kyc_submissions
    FOR EACH ROW EXECUTE FUNCTION fn_rzp_kyc_submissions_touch();


-- ── RLS: service-role only (managed via Python service layer) ───────────────
ALTER TABLE rzp_kyc_submissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE rzp_kyc_batches     ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    CREATE POLICY rzp_kyc_submissions_service_all ON rzp_kyc_submissions
        FOR ALL TO PUBLIC USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY rzp_kyc_batches_service_all ON rzp_kyc_batches
        FOR ALL TO PUBLIC USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
