-- ============================================================
-- Migration 030: Reconciliation Engine
-- Purpose:
--   1. webhook_events       — durable ledger of every gateway callback,
--                             for replay-safe processing + audit trail
--   2. reconciliation_runs  — header for each scan (date, scope, summary)
--   3. reconciliation_discrepancies — one row per detected mismatch
--   4. Performance indexes on payments / orders for the recon scans
--
-- All statements are idempotent (IF NOT EXISTS).  Safe to re-run.
-- ============================================================

-- ── 1. WEBHOOK EVENTS (durable replay-safe ledger) ───────────
-- Every payment-gateway callback is stored here BEFORE processing.
-- Replaces the volatile Redis idempotency check so a Redis flush
-- cannot cause a duplicate order / payment update.
CREATE TABLE IF NOT EXISTS webhook_events (
    id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Source identification
    gateway            VARCHAR(50)  NOT NULL,    -- razorpay | cashfree | phonepe | payu | paytm
    event_type         VARCHAR(100) NOT NULL,    -- payment.captured | payment.failed | refund.processed | settlement.processed | ...
    -- Gateway-supplied identifiers
    event_id           VARCHAR(255),             -- gateway's unique event id (X-Razorpay-Event-Id etc.)
    gateway_payment_id VARCHAR(255),             -- e.g. razorpay payment id
    gateway_order_id   VARCHAR(255),             -- e.g. razorpay order id
    -- Tenant scoping (filled in once we resolve the linked payment)
    user_id            TEXT,
    restaurant_id      UUID,
    branch_id          UUID,
    -- Linked entities (filled in after resolution)
    payment_id         UUID         REFERENCES payments(id) ON DELETE SET NULL,
    order_id           UUID         REFERENCES orders(id)   ON DELETE SET NULL,
    -- Raw envelope + signature for forensic replay
    raw_payload        JSONB        NOT NULL,
    signature          TEXT,
    signature_valid    BOOLEAN      NOT NULL DEFAULT false,
    -- Processing state machine: received -> processing -> processed | failed | skipped
    status             VARCHAR(30)  NOT NULL DEFAULT 'received'
        CHECK (status IN ('received','processing','processed','failed','skipped','duplicate')),
    error_message      TEXT,
    attempts           INTEGER      NOT NULL DEFAULT 0,
    received_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    processed_at       TIMESTAMPTZ,
    -- Idempotency: same (gateway, event_id) pair processed exactly once
    CONSTRAINT uq_webhook_events_gateway_event UNIQUE (gateway, event_id)
);

COMMENT ON TABLE webhook_events IS
    'Durable webhook ledger.  Every callback from a payment gateway is inserted
     here before being applied to payments/orders.  Provides:
       * replay-safe idempotency surviving Redis flushes
       * complete forensic audit trail
       * basis for the "webhook delay/failure" reconciliation check.';

CREATE INDEX IF NOT EXISTS idx_webhook_events_status_received
    ON webhook_events (status, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_webhook_events_gateway_payment
    ON webhook_events (gateway, gateway_payment_id) WHERE gateway_payment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_webhook_events_payment
    ON webhook_events (payment_id) WHERE payment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_webhook_events_order
    ON webhook_events (order_id) WHERE order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_webhook_events_user_received
    ON webhook_events (user_id, received_at DESC) WHERE user_id IS NOT NULL;


-- ── 2. RECONCILIATION RUNS (header per scan) ─────────────────
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id                       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                  TEXT        NOT NULL,        -- owner (billing-account scope)
    restaurant_id            UUID,
    branch_id                UUID,
    -- Scan window (UTC)
    period_start             TIMESTAMPTZ NOT NULL,
    period_end               TIMESTAMPTZ NOT NULL,
    -- Aggregate counts
    orders_scanned           INTEGER     NOT NULL DEFAULT 0,
    payments_scanned         INTEGER     NOT NULL DEFAULT 0,
    settlements_scanned      INTEGER     NOT NULL DEFAULT 0,
    discrepancies_found      INTEGER     NOT NULL DEFAULT 0,
    -- Aggregate amounts (for executive summary)
    total_order_amount       NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_payment_amount     NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_settled_amount     NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_unsettled_amount   NUMERIC(14,2) NOT NULL DEFAULT 0,
    -- Status: running -> completed | failed
    status                   VARCHAR(20) NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','completed','failed')),
    triggered_by             TEXT        NOT NULL DEFAULT 'system',
    started_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at             TIMESTAMPTZ,
    error_message            TEXT
);

CREATE INDEX IF NOT EXISTS idx_recon_runs_user_started
    ON reconciliation_runs (user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_recon_runs_status
    ON reconciliation_runs (status, started_at DESC);


-- ── 3. RECONCILIATION DISCREPANCIES (detail rows) ────────────
-- One row per detected mismatch.  The kind column drives UI grouping.
CREATE TABLE IF NOT EXISTS reconciliation_discrepancies (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id            UUID        NOT NULL REFERENCES reconciliation_runs(id) ON DELETE CASCADE,
    user_id           TEXT        NOT NULL,
    restaurant_id     UUID,
    branch_id         UUID,
    -- The 6 mismatch scenarios from the product spec
    kind              VARCHAR(60) NOT NULL
        CHECK (kind IN (
            'payment_received_order_not_updated',
            'order_created_payment_missing',
            'duplicate_payment',
            'failed_settlement',
            'partial_settlement',
            'webhook_delayed_or_failed',
            'amount_mismatch',
            'orphan_settlement'
        )),
    severity          VARCHAR(20) NOT NULL DEFAULT 'warning'
        CHECK (severity IN ('info','warning','critical')),
    -- Linked entities (any/all may be NULL depending on kind)
    order_id          UUID,
    payment_id        UUID,
    settlement_id     UUID,
    customer_id       INTEGER,
    -- Context
    expected_amount   NUMERIC(14,2),
    actual_amount     NUMERIC(14,2),
    delta_amount      NUMERIC(14,2),
    description       TEXT        NOT NULL,
    metadata          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- Resolution
    status            VARCHAR(20) NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','acknowledged','resolved','ignored')),
    resolved_by       TEXT,
    resolved_at       TIMESTAMPTZ,
    resolution_notes  TEXT,
    detected_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_recon_disc_user_status
    ON reconciliation_discrepancies (user_id, status, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_recon_disc_run
    ON reconciliation_discrepancies (run_id);
CREATE INDEX IF NOT EXISTS idx_recon_disc_kind
    ON reconciliation_discrepancies (user_id, kind, status);
CREATE INDEX IF NOT EXISTS idx_recon_disc_order
    ON reconciliation_discrepancies (order_id) WHERE order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_recon_disc_payment
    ON reconciliation_discrepancies (payment_id) WHERE payment_id IS NOT NULL;


-- ── 4. SUPPORT INDEXES for fast recon scans ──────────────────
-- Find paid orders that didn't get marked Confirmed/Completed
CREATE INDEX IF NOT EXISTS idx_payments_status_paid_at
    ON payments (status, paid_at DESC) WHERE paid_at IS NOT NULL;

-- Find orders without payments
CREATE INDEX IF NOT EXISTS idx_orders_user_created_status
    ON orders (user_id, created_at DESC, status);

-- Duplicate payment detection (multiple completed payments per order)
CREATE INDEX IF NOT EXISTS idx_payments_order_status
    ON payments (order_id, status);
