-- ============================================================================
-- Migration 060 — Razorpay deep integration (Phase 1: Foundation)
--
-- Adds the gateway-mirror schema for the deep Razorpay integration. This
-- migration is additive and isolated under the `rzp_` prefix; nothing in
-- existing tables is modified. Wiring into payments / orders / ledgers is
-- delivered in subsequent phases.
--
-- Tables:
--   rzp_orders              internal_order ↔ razorpay_order
--   rzp_payments            full payment mirror (partitioned monthly)
--   rzp_payment_status_log  immutable status transitions per payment
--   rzp_qr_codes            QR objects we created on Razorpay
--   rzp_qr_order_links      QR ↔ internal_order ↔ rzp_order
--   rzp_geo_validations     pre-capture geo-fence checks
--   rzp_refunds             refund mirror
--   rzp_disputes            dispute mirror + evidence pointer
--   rzp_settlements         settlement headers
--   rzp_settlement_payments payment-↔-settlement allocation
--   rzp_route_accounts      Razorpay Route linked accounts
--   rzp_route_transfers     split transfers per payment
--   rzp_smart_collect_va    virtual accounts
--   rzp_smart_collect_txn   inbound NEFT/UPI/IMPS to VAs
--   rzp_invoices            hosted invoices
--   rzp_api_calls           outbound gateway call audit (partitioned)
--   rzp_idempotency_keys    caller-side idempotency for our checkout API
--
-- Append-only invariants are enforced by triggers for:
--   rzp_payment_status_log, rzp_api_calls, rzp_settlement_payments
-- ============================================================================

BEGIN;

-- ─────────────────────────── enums ────────────────────────────────────────

DO $$ BEGIN
    CREATE TYPE rzp_order_state AS ENUM (
        'created', 'attempted', 'paid', 'expired', 'cancelled', 'failed'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE rzp_payment_state AS ENUM (
        'created', 'authorized', 'captured', 'refunded', 'failed'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE rzp_qr_state AS ENUM (
        'active', 'closed', 'expired'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE rzp_refund_state AS ENUM (
        'pending', 'processed', 'failed'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE rzp_dispute_state AS ENUM (
        'open', 'under_review', 'won', 'lost', 'closed'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE rzp_settlement_state AS ENUM (
        'pending', 'processing', 'processed', 'failed', 'reversed'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE rzp_route_account_state AS ENUM (
        'created', 'activated', 'suspended', 'rejected', 'deleted'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE rzp_route_transfer_state AS ENUM (
        'created', 'processed', 'reversed', 'failed'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE rzp_va_state AS ENUM (
        'active', 'closed'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE rzp_invoice_state AS ENUM (
        'draft', 'issued', 'partially_paid', 'paid', 'expired', 'cancelled'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE rzp_api_call_state AS ENUM (
        'pending', 'succeeded', 'failed', 'retrying'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;


-- ─────────────────────────── 1. rzp_orders ────────────────────────────────
-- Maps our internal POS order id to a Razorpay order resource.

CREATE TABLE IF NOT EXISTS rzp_orders (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id         UUID         NOT NULL,
    branch_id           UUID,
    internal_order_id   UUID         NOT NULL,
    razorpay_order_id   TEXT         NOT NULL UNIQUE,
    receipt             TEXT,
    amount_paise        BIGINT       NOT NULL,
    amount_paid_paise   BIGINT       NOT NULL DEFAULT 0,
    amount_due_paise    BIGINT       NOT NULL,
    currency            CHAR(3)      NOT NULL DEFAULT 'INR',
    status              rzp_order_state NOT NULL DEFAULT 'created',
    notes               JSONB        NOT NULL DEFAULT '{}'::jsonb,
    raw_response        JSONB,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (merchant_id, internal_order_id)
);

CREATE INDEX IF NOT EXISTS ix_rzp_orders_merchant_created
    ON rzp_orders (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_rzp_orders_internal_order
    ON rzp_orders (internal_order_id);
CREATE INDEX IF NOT EXISTS ix_rzp_orders_status
    ON rzp_orders (status) WHERE status IN ('created', 'attempted');


-- ─────────────────────────── 2. rzp_payments ──────────────────────────────
-- Mirror of every payment object Razorpay tells us about.
-- Partitioned by created_at monthly + DEFAULT (matches pattern in 037/053).

CREATE TABLE IF NOT EXISTS rzp_payments (
    id                  UUID         NOT NULL DEFAULT gen_random_uuid(),
    razorpay_payment_id TEXT         NOT NULL,
    razorpay_order_id   TEXT,                                -- nullable: smart-collect/route-only payments
    rzp_order_uuid      UUID,                                -- FK-style ref to rzp_orders.id
    merchant_id         UUID         NOT NULL,
    branch_id           UUID,
    internal_order_id   UUID,
    amount_paise        BIGINT       NOT NULL,
    fee_paise           BIGINT,
    tax_paise           BIGINT,
    currency            CHAR(3)      NOT NULL DEFAULT 'INR',
    method              TEXT,                                -- card|netbanking|wallet|upi|emi|...
    upi_vpa             TEXT,
    bank_reference      TEXT,
    acquirer_data       JSONB,
    status              rzp_payment_state NOT NULL,
    error_code          TEXT,
    error_description   TEXT,
    captured            BOOLEAN      NOT NULL DEFAULT FALSE,
    captured_at         TIMESTAMPTZ,
    raw_payload         JSONB        NOT NULL,
    signature           TEXT,
    notes               JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

CREATE TABLE IF NOT EXISTS rzp_payments_default
    PARTITION OF rzp_payments DEFAULT;

-- Cross-partition uniqueness companion (cannot put UNIQUE on partitioned table
-- without including the partition key — same trick as merchant_ledger).
CREATE TABLE IF NOT EXISTS rzp_payments_index (
    razorpay_payment_id TEXT PRIMARY KEY,
    payment_uuid        UUID NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_rzp_payments_merchant_created
    ON rzp_payments (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_rzp_payments_order
    ON rzp_payments (razorpay_order_id);
CREATE INDEX IF NOT EXISTS ix_rzp_payments_internal_order
    ON rzp_payments (internal_order_id);
CREATE INDEX IF NOT EXISTS ix_rzp_payments_status
    ON rzp_payments (status, created_at DESC);


-- ──────────────────── 3. rzp_payment_status_log ───────────────────────────
-- Append-only state-transition log per payment.

CREATE TABLE IF NOT EXISTS rzp_payment_status_log (
    id                  BIGSERIAL PRIMARY KEY,
    razorpay_payment_id TEXT         NOT NULL,
    from_status         rzp_payment_state,
    to_status           rzp_payment_state NOT NULL,
    reason              TEXT,
    raw_payload         JSONB,
    correlation_id      TEXT,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_rzp_pay_status_log_pid_created
    ON rzp_payment_status_log (razorpay_payment_id, created_at);

CREATE OR REPLACE FUNCTION fn_rzp_pay_status_block_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'rzp_payment_status_log is append-only (% denied)', TG_OP;
END $$;

DROP TRIGGER IF EXISTS trg_rzp_pay_status_no_update ON rzp_payment_status_log;
CREATE TRIGGER trg_rzp_pay_status_no_update
    BEFORE UPDATE OR DELETE ON rzp_payment_status_log
    FOR EACH ROW EXECUTE FUNCTION fn_rzp_pay_status_block_mutation();


-- ─────────────────────────── 4. rzp_qr_codes ──────────────────────────────

CREATE TABLE IF NOT EXISTS rzp_qr_codes (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    qr_id               TEXT         NOT NULL UNIQUE,           -- Razorpay qr_HXz...
    merchant_id         UUID         NOT NULL,
    branch_id           UUID,
    name                TEXT,
    type                TEXT         NOT NULL DEFAULT 'upi_qr',
    usage               TEXT         NOT NULL DEFAULT 'single_use',
    fixed_amount        BOOLEAN      NOT NULL DEFAULT TRUE,
    amount_paise        BIGINT,
    description         TEXT,
    image_url           TEXT,
    image_content       TEXT,                                   -- raw upi:// content if returned
    status              rzp_qr_state NOT NULL DEFAULT 'active',
    close_by            TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    close_reason        TEXT,
    payments_amount_received_paise BIGINT NOT NULL DEFAULT 0,
    payments_count_received        INT     NOT NULL DEFAULT 0,
    notes               JSONB        NOT NULL DEFAULT '{}'::jsonb,
    raw_response        JSONB,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_rzp_qr_merchant_status
    ON rzp_qr_codes (merchant_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_rzp_qr_close_by
    ON rzp_qr_codes (close_by) WHERE status = 'active';


-- ─────────────────── 5. rzp_qr_order_links ────────────────────────────────
-- Many-to-one: a QR may collect multiple payments / be linked to an order.

CREATE TABLE IF NOT EXISTS rzp_qr_order_links (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    qr_id               TEXT         NOT NULL REFERENCES rzp_qr_codes (qr_id) ON DELETE CASCADE,
    rzp_order_uuid      UUID         REFERENCES rzp_orders (id) ON DELETE SET NULL,
    razorpay_order_id   TEXT,
    internal_order_id   UUID         NOT NULL,
    merchant_id         UUID         NOT NULL,
    is_primary          BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (qr_id, internal_order_id)
);

CREATE INDEX IF NOT EXISTS ix_rzp_qr_links_internal_order
    ON rzp_qr_order_links (internal_order_id);
CREATE INDEX IF NOT EXISTS ix_rzp_qr_links_rzp_order
    ON rzp_qr_order_links (razorpay_order_id);


-- ─────────────────────── 6. rzp_geo_validations ───────────────────────────
-- One row per geo-fence check performed before payment confirmation.

CREATE TABLE IF NOT EXISTS rzp_geo_validations (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id             UUID         NOT NULL,
    branch_id               UUID,
    internal_order_id       UUID         NOT NULL,
    razorpay_order_id       TEXT,
    customer_lat            NUMERIC(10,7) NOT NULL,
    customer_lon            NUMERIC(10,7) NOT NULL,
    merchant_lat            NUMERIC(10,7) NOT NULL,
    merchant_lon            NUMERIC(10,7) NOT NULL,
    distance_meters         NUMERIC(12,3) NOT NULL,
    radius_meters           INT          NOT NULL,
    within_radius           BOOLEAN      NOT NULL,
    spoof_flags             JSONB        NOT NULL DEFAULT '[]'::jsonb,
    location_age_seconds    INT,
    accuracy_meters         NUMERIC(8,2),
    user_agent              TEXT,
    ip_address              INET,
    verification_passed     BOOLEAN      NOT NULL,
    fail_reason             TEXT,
    verified_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_rzp_geo_order ON rzp_geo_validations (internal_order_id);
CREATE INDEX IF NOT EXISTS ix_rzp_geo_merchant_created ON rzp_geo_validations (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_rzp_geo_failed ON rzp_geo_validations (verification_passed, created_at DESC) WHERE verification_passed = FALSE;


-- ─────────────────────────── 7. rzp_refunds ───────────────────────────────

CREATE TABLE IF NOT EXISTS rzp_refunds (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    refund_id           TEXT         NOT NULL UNIQUE,            -- rfnd_xxx
    razorpay_payment_id TEXT         NOT NULL,
    internal_refund_id  UUID,                                    -- maps to refunds.id
    merchant_id         UUID         NOT NULL,
    amount_paise        BIGINT       NOT NULL,
    currency            CHAR(3)      NOT NULL DEFAULT 'INR',
    speed_requested     TEXT,
    speed_processed     TEXT,
    status              rzp_refund_state NOT NULL DEFAULT 'pending',
    reason              TEXT,
    initiated_by        UUID,
    batch_id            TEXT,
    acquirer_data       JSONB,
    notes               JSONB        NOT NULL DEFAULT '{}'::jsonb,
    raw_payload         JSONB,
    processed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_rzp_refunds_merchant_created
    ON rzp_refunds (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_rzp_refunds_payment
    ON rzp_refunds (razorpay_payment_id);
CREATE INDEX IF NOT EXISTS ix_rzp_refunds_internal
    ON rzp_refunds (internal_refund_id);


-- ─────────────────────────── 8. rzp_disputes ──────────────────────────────

CREATE TABLE IF NOT EXISTS rzp_disputes (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispute_id          TEXT         NOT NULL UNIQUE,            -- disp_xxx
    razorpay_payment_id TEXT         NOT NULL,
    internal_dispute_id UUID,                                    -- maps to disputes.id
    merchant_id         UUID         NOT NULL,
    amount_paise        BIGINT       NOT NULL,
    currency            CHAR(3)      NOT NULL DEFAULT 'INR',
    reason_code         TEXT,
    reason_description  TEXT,
    phase               TEXT,                                    -- chargeback|fraud|retrieval|pre_arbitration|arbitration
    status              rzp_dispute_state NOT NULL DEFAULT 'open',
    deadline_at         TIMESTAMPTZ,
    evidence            JSONB        NOT NULL DEFAULT '{}'::jsonb,
    raw_payload         JSONB,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_rzp_disputes_merchant_status
    ON rzp_disputes (merchant_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_rzp_disputes_deadline
    ON rzp_disputes (deadline_at) WHERE status IN ('open', 'under_review');


-- ─────────────────────── 9. rzp_settlements ───────────────────────────────

CREATE TABLE IF NOT EXISTS rzp_settlements (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    settlement_id       TEXT         NOT NULL UNIQUE,            -- setl_xxx
    merchant_id         UUID         NOT NULL,                   -- merchant whose linked-account settled
    linked_account_id   TEXT,                                    -- acc_xxx (null for primary account)
    amount_paise        BIGINT       NOT NULL,
    fees_paise          BIGINT       NOT NULL DEFAULT 0,
    tax_paise           BIGINT       NOT NULL DEFAULT 0,
    utr                 TEXT,
    status              rzp_settlement_state NOT NULL DEFAULT 'pending',
    settled_at          TIMESTAMPTZ,
    created_for_date    DATE,
    raw_payload         JSONB,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_rzp_settlements_merchant_status
    ON rzp_settlements (merchant_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_rzp_settlements_utr
    ON rzp_settlements (utr) WHERE utr IS NOT NULL;


-- ──────────────────── 10. rzp_settlement_payments ─────────────────────────
-- Append-only mapping of which payments rolled into which settlement.

CREATE TABLE IF NOT EXISTS rzp_settlement_payments (
    id                  BIGSERIAL PRIMARY KEY,
    settlement_id       TEXT         NOT NULL,
    razorpay_payment_id TEXT         NOT NULL,
    merchant_id         UUID         NOT NULL,
    type                TEXT         NOT NULL,                   -- payment|refund|adjustment|dispute|commission
    amount_paise        BIGINT       NOT NULL,
    fee_paise           BIGINT       NOT NULL DEFAULT 0,
    tax_paise           BIGINT       NOT NULL DEFAULT 0,
    debit_paise         BIGINT       NOT NULL DEFAULT 0,
    credit_paise        BIGINT       NOT NULL DEFAULT 0,
    raw_row             JSONB,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (settlement_id, razorpay_payment_id, type)
);

CREATE INDEX IF NOT EXISTS ix_rzp_setl_pay_settlement
    ON rzp_settlement_payments (settlement_id);
CREATE INDEX IF NOT EXISTS ix_rzp_setl_pay_payment
    ON rzp_settlement_payments (razorpay_payment_id);

CREATE OR REPLACE FUNCTION fn_rzp_setl_pay_block_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'rzp_settlement_payments is append-only (% denied)', TG_OP;
END $$;

DROP TRIGGER IF EXISTS trg_rzp_setl_pay_no_mutate ON rzp_settlement_payments;
CREATE TRIGGER trg_rzp_setl_pay_no_mutate
    BEFORE UPDATE OR DELETE ON rzp_settlement_payments
    FOR EACH ROW EXECUTE FUNCTION fn_rzp_setl_pay_block_mutation();


-- ───────────────────────── 11. rzp_route_accounts ─────────────────────────

CREATE TABLE IF NOT EXISTS rzp_route_accounts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    linked_account_id   TEXT         NOT NULL UNIQUE,            -- acc_xxx
    merchant_id         UUID         NOT NULL UNIQUE,            -- one linked account per merchant for now
    legal_business_name TEXT,
    business_type       TEXT,
    contact_name        TEXT,
    email               TEXT,
    phone               TEXT,
    reference_id        TEXT,
    kyc_status          TEXT,                                    -- created|under_review|activated|needs_clarification|rejected
    activation_status   TEXT,
    status              rzp_route_account_state NOT NULL DEFAULT 'created',
    bank_account_ifsc   TEXT,
    bank_account_last4  CHAR(4),
    bank_account_hash   TEXT,                                    -- sha256(account_number) — NEVER expose
    notes               JSONB        NOT NULL DEFAULT '{}'::jsonb,
    raw_payload         JSONB,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_rzp_route_accounts_status
    ON rzp_route_accounts (status, kyc_status);


-- ───────────────────────── 12. rzp_route_transfers ────────────────────────

CREATE TABLE IF NOT EXISTS rzp_route_transfers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transfer_id         TEXT         NOT NULL UNIQUE,            -- trf_xxx
    razorpay_payment_id TEXT         NOT NULL,
    source_account_id   TEXT,                                    -- platform acc, null = platform
    recipient_account_id TEXT        NOT NULL,                   -- acc_xxx
    merchant_id         UUID         NOT NULL,                   -- merchant being paid out
    amount_paise        BIGINT       NOT NULL,
    currency            CHAR(3)      NOT NULL DEFAULT 'INR',
    on_hold             BOOLEAN      NOT NULL DEFAULT FALSE,
    on_hold_until       TIMESTAMPTZ,
    fee_paise           BIGINT,
    tax_paise           BIGINT,
    status              rzp_route_transfer_state NOT NULL DEFAULT 'created',
    notes               JSONB        NOT NULL DEFAULT '{}'::jsonb,
    raw_payload         JSONB,
    processed_at        TIMESTAMPTZ,
    reversed_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_rzp_transfers_payment
    ON rzp_route_transfers (razorpay_payment_id);
CREATE INDEX IF NOT EXISTS ix_rzp_transfers_recipient
    ON rzp_route_transfers (recipient_account_id, status);
CREATE INDEX IF NOT EXISTS ix_rzp_transfers_merchant
    ON rzp_route_transfers (merchant_id, created_at DESC);


-- ───────────────────────── 13. rzp_smart_collect_va ───────────────────────

CREATE TABLE IF NOT EXISTS rzp_smart_collect_va (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    virtual_account_id  TEXT         NOT NULL UNIQUE,            -- va_xxx
    merchant_id         UUID         NOT NULL,
    branch_id           UUID,
    customer_id         TEXT,                                    -- cust_xxx
    name                TEXT,
    description         TEXT,
    receivers           JSONB        NOT NULL DEFAULT '[]'::jsonb,  -- list of {type:bank_account|vpa, account_number/vpa}
    allowed_payers      JSONB        NOT NULL DEFAULT '[]'::jsonb,
    status              rzp_va_state NOT NULL DEFAULT 'active',
    amount_paid_paise   BIGINT       NOT NULL DEFAULT 0,
    amount_expected_paise BIGINT,
    close_by            TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    notes               JSONB        NOT NULL DEFAULT '{}'::jsonb,
    raw_payload         JSONB,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_rzp_va_merchant_status
    ON rzp_smart_collect_va (merchant_id, status, created_at DESC);


-- ───────────────────────── 14. rzp_smart_collect_txn ──────────────────────

CREATE TABLE IF NOT EXISTS rzp_smart_collect_txn (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    razorpay_payment_id TEXT         NOT NULL UNIQUE,
    virtual_account_id  TEXT         NOT NULL,
    merchant_id         UUID         NOT NULL,
    amount_paise        BIGINT       NOT NULL,
    currency            CHAR(3)      NOT NULL DEFAULT 'INR',
    method              TEXT,                                    -- bank_transfer|upi
    upi_payer_vpa       TEXT,
    payer_name          TEXT,
    payer_account_number TEXT,
    payer_ifsc          TEXT,
    bank_reference      TEXT,
    transfer_mode       TEXT,                                    -- NEFT|RTGS|IMPS|UPI
    reconciled          BOOLEAN      NOT NULL DEFAULT FALSE,
    reconciled_at       TIMESTAMPTZ,
    raw_payload         JSONB,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_rzp_sc_txn_va
    ON rzp_smart_collect_txn (virtual_account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_rzp_sc_txn_merchant
    ON rzp_smart_collect_txn (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_rzp_sc_txn_unreconciled
    ON rzp_smart_collect_txn (reconciled, created_at) WHERE reconciled = FALSE;


-- ───────────────────────── 15. rzp_invoices ───────────────────────────────

CREATE TABLE IF NOT EXISTS rzp_invoices (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id          TEXT         NOT NULL UNIQUE,            -- inv_xxx
    merchant_id         UUID         NOT NULL,
    branch_id           UUID,
    internal_order_id   UUID,
    invoice_number      TEXT,
    customer_id         TEXT,
    customer_details    JSONB,
    amount_paise        BIGINT       NOT NULL,
    amount_paid_paise   BIGINT       NOT NULL DEFAULT 0,
    amount_due_paise    BIGINT       NOT NULL,
    currency            CHAR(3)      NOT NULL DEFAULT 'INR',
    status              rzp_invoice_state NOT NULL DEFAULT 'draft',
    short_url           TEXT,
    description         TEXT,
    expire_by           TIMESTAMPTZ,
    issued_at           TIMESTAMPTZ,
    paid_at             TIMESTAMPTZ,
    cancelled_at        TIMESTAMPTZ,
    razorpay_order_id   TEXT,
    line_items          JSONB        NOT NULL DEFAULT '[]'::jsonb,
    raw_payload         JSONB,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_rzp_invoices_merchant_status
    ON rzp_invoices (merchant_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_rzp_invoices_internal_order
    ON rzp_invoices (internal_order_id);


-- ───────────────────────── 16. rzp_api_calls ──────────────────────────────
-- Forensic audit for every outbound Razorpay call. Idempotency key store too.
-- Partitioned by created_at monthly + DEFAULT.

CREATE TABLE IF NOT EXISTS rzp_api_calls (
    id                  UUID         NOT NULL DEFAULT gen_random_uuid(),
    merchant_id         UUID,
    operation           TEXT         NOT NULL,                   -- 'orders.create'|'qr_codes.create'|...
    method              TEXT         NOT NULL,                   -- POST|GET|PATCH
    path                TEXT         NOT NULL,                   -- /v1/orders
    idempotency_key     TEXT,                                    -- our caller-supplied key (if any)
    request_body        JSONB,
    request_headers     JSONB,
    response_status     INT,
    response_body       JSONB,
    response_headers    JSONB,
    error_code          TEXT,
    error_message       TEXT,
    state               rzp_api_call_state NOT NULL DEFAULT 'pending',
    attempt             INT          NOT NULL DEFAULT 1,
    duration_ms         INT,
    correlation_id      TEXT,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

CREATE TABLE IF NOT EXISTS rzp_api_calls_default
    PARTITION OF rzp_api_calls DEFAULT;

-- Cross-partition unique idempotency-key store
CREATE TABLE IF NOT EXISTS rzp_api_idempotency (
    operation       TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    api_call_id     UUID NOT NULL,
    response_body   JSONB,
    response_status INT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (operation, idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_rzp_api_calls_op_created
    ON rzp_api_calls (operation, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_rzp_api_calls_correlation
    ON rzp_api_calls (correlation_id) WHERE correlation_id IS NOT NULL;

-- Append-only triggers (only state/response columns are updatable)
CREATE OR REPLACE FUNCTION fn_rzp_api_calls_block_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'rzp_api_calls is append-only (DELETE denied)';
    END IF;
    -- allow updates only of state/response/error/duration/completed_at/attempt
    IF NEW.id              IS DISTINCT FROM OLD.id              OR
       NEW.merchant_id     IS DISTINCT FROM OLD.merchant_id     OR
       NEW.operation       IS DISTINCT FROM OLD.operation       OR
       NEW.method          IS DISTINCT FROM OLD.method          OR
       NEW.path            IS DISTINCT FROM OLD.path            OR
       NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key OR
       NEW.request_body    IS DISTINCT FROM OLD.request_body    OR
       NEW.request_headers IS DISTINCT FROM OLD.request_headers OR
       NEW.correlation_id  IS DISTINCT FROM OLD.correlation_id  OR
       NEW.created_at      IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'rzp_api_calls: immutable column changed';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_rzp_api_calls_guard ON rzp_api_calls;
CREATE TRIGGER trg_rzp_api_calls_guard
    BEFORE UPDATE OR DELETE ON rzp_api_calls
    FOR EACH ROW EXECUTE FUNCTION fn_rzp_api_calls_block_mutation();


-- ─────────────────────── 17. rzp_idempotency_keys ─────────────────────────
-- Caller-side idempotency keys for our /api/v1/orders/checkout etc. endpoints,
-- so a retried HTTP call returns the previously-recorded response and never
-- creates a duplicate Razorpay order.

CREATE TABLE IF NOT EXISTS rzp_idempotency_keys (
    scope               TEXT         NOT NULL,                   -- 'checkout'|'refund'|'qr_create'
    idempotency_key     TEXT         NOT NULL,
    merchant_id         UUID         NOT NULL,
    user_id             UUID,
    request_hash        TEXT         NOT NULL,                   -- sha256(canonical request) — replay-protection
    response_body       JSONB,
    response_status     INT,
    state               TEXT         NOT NULL DEFAULT 'in_flight',  -- in_flight|completed|failed
    locked_until        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scope, merchant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_rzp_idem_state
    ON rzp_idempotency_keys (state, created_at);


-- ─────────────────────── permissions seed ─────────────────────────────────
-- Mint razorpay-namespace permissions; role grants are wired in a later phase.

INSERT INTO permissions (key) VALUES
    ('razorpay.orders.read'),
    ('razorpay.orders.write'),
    ('razorpay.payments.read'),
    ('razorpay.payments.capture'),
    ('razorpay.qr.read'),
    ('razorpay.qr.write'),
    ('razorpay.refunds.read'),
    ('razorpay.refunds.write'),
    ('razorpay.disputes.read'),
    ('razorpay.disputes.write'),
    ('razorpay.settlements.read'),
    ('razorpay.route.read'),
    ('razorpay.route.write'),
    ('razorpay.route.admin'),
    ('razorpay.smart_collect.read'),
    ('razorpay.smart_collect.write'),
    ('razorpay.invoices.read'),
    ('razorpay.invoices.write'),
    ('razorpay.recon.read'),
    ('razorpay.recon.run'),
    ('razorpay.admin')
ON CONFLICT (key) DO NOTHING;

COMMIT;
