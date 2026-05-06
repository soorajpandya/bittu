-- ============================================================================
-- Migration 028: Statement & Settlement Module
--
-- Merchant-facing settlement experience (Razorpay/PhonePe-style dashboard).
-- Tracks how much Bittu collects, fee deductions (0.15% + 18% GST), and
-- when net amount settles to the merchant's bank account.
--
-- SAFE: All operations are ADDITIVE only.
--   - Adds bittu_settlements table
--   - Adds bittu_settlement_transactions table
--   - Adds bittu_settlement_timeline table (immutable audit trail)
--   - Adds permissions for statements.* module
--   - Adds performance indexes
--   - Does NOT touch existing pg_settlements, payments, or accounting tables
-- ============================================================================

BEGIN;

-- ════════════════════════════════════════════════════════════════════════════
-- 1. BITTU SETTLEMENTS
--
--    Tracks each settlement batch from Bittu to merchant bank.
--    Merchant pays 0.15% Bittu platform fee + 18% GST on fee.
--    Net = Gross - bittu_fee - gst_on_fee
--
--    Lifecycle:
--      pending → processing → sent_to_bank → settled
--                                          → failed → (retry) → ...
--                          → reversed
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS bittu_settlements (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id           UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id               UUID REFERENCES sub_branches(id) ON DELETE SET NULL,

    -- Human-readable reference (e.g. STL-20260506-0001)
    settlement_reference    VARCHAR(100) NOT NULL UNIQUE,

    -- Amounts — all NUMERIC for finance-grade precision
    gross_amount            NUMERIC(14,2) NOT NULL CHECK (gross_amount > 0),
    bittu_fee_amount        NUMERIC(14,6) NOT NULL DEFAULT 0 CHECK (bittu_fee_amount >= 0),
    gst_amount              NUMERIC(14,6) NOT NULL DEFAULT 0 CHECK (gst_amount >= 0),
    net_settlement_amount   NUMERIC(14,2) NOT NULL CHECK (net_settlement_amount >= 0),

    -- Fee rates stored at time of creation (immutable audit)
    fee_rate                NUMERIC(8,6)  NOT NULL DEFAULT 0.001500,  -- 0.15%
    gst_rate                NUMERIC(8,6)  NOT NULL DEFAULT 0.180000,  -- 18%

    -- Status machine
    settlement_status       VARCHAR(30) NOT NULL DEFAULT 'pending'
                            CHECK (settlement_status IN (
                                'pending', 'processing', 'sent_to_bank',
                                'settled', 'failed', 'reversed'
                            )),

    -- Timing
    settlement_cycle        VARCHAR(10) NOT NULL DEFAULT 'T+1'
                            CHECK (settlement_cycle IN ('T+0', 'T+1')),
    expected_settlement_at  TIMESTAMPTZ,
    settled_at              TIMESTAMPTZ,

    -- Bank payout reference (from payment gateway or internal transfer)
    bank_reference_number   VARCHAR(200),

    -- Retry tracking for failed settlements
    retry_count             SMALLINT NOT NULL DEFAULT 0,
    failure_reason          TEXT,
    last_attempt_at         TIMESTAMPTZ,

    -- Accounting journal link (created when settled)
    journal_entry_id        UUID,  -- references journal_entries(id) — soft FK to avoid hard dep

    -- Covers period
    period_start            TIMESTAMPTZ,
    period_end              TIMESTAMPTZ,

    -- Idempotency: prevent duplicate settlement for same payment batch
    idempotency_key         VARCHAR(200) UNIQUE,

    -- Extra structured data
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Primary lookup: by restaurant + status + date
CREATE INDEX IF NOT EXISTS idx_bittu_settlements_restaurant
    ON bittu_settlements(restaurant_id, settlement_status, created_at DESC);

-- Branch-scoped queries
CREATE INDEX IF NOT EXISTS idx_bittu_settlements_branch
    ON bittu_settlements(restaurant_id, branch_id, settlement_status, created_at DESC);

-- Status-specific monitoring
CREATE INDEX IF NOT EXISTS idx_bittu_settlements_status_time
    ON bittu_settlements(settlement_status, expected_settlement_at)
    WHERE settlement_status IN ('pending', 'processing', 'sent_to_bank');

-- Reference lookup
CREATE INDEX IF NOT EXISTS idx_bittu_settlements_reference
    ON bittu_settlements(settlement_reference);

-- ETA queries (mobile: "next payout")
CREATE INDEX IF NOT EXISTS idx_bittu_settlements_eta
    ON bittu_settlements(restaurant_id, expected_settlement_at)
    WHERE settlement_status NOT IN ('settled', 'reversed', 'failed');


-- ════════════════════════════════════════════════════════════════════════════
-- 2. BITTU SETTLEMENT TRANSACTIONS
--
--    Maps individual payments/orders to their settlement batch.
--    Stores per-transaction fee breakdown for the statement detail view.
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS bittu_settlement_transactions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    settlement_id       UUID NOT NULL REFERENCES bittu_settlements(id) ON DELETE CASCADE,
    restaurant_id       UUID NOT NULL,
    branch_id           UUID,

    -- Source identifiers
    payment_id          UUID,       -- references payments(id) — soft FK
    order_id            UUID,       -- references orders(id) — soft FK

    -- Per-transaction amounts
    gross_amount        NUMERIC(14,2) NOT NULL CHECK (gross_amount != 0),
    fee_amount          NUMERIC(14,6) NOT NULL DEFAULT 0,
    gst_amount          NUMERIC(14,6) NOT NULL DEFAULT 0,
    net_amount          NUMERIC(14,2) NOT NULL,

    -- Transaction type: payment, refund, reversal
    transaction_type    VARCHAR(20) NOT NULL DEFAULT 'payment'
                        CHECK (transaction_type IN ('payment', 'refund', 'reversal', 'adjustment')),

    -- Denormalised at insert time for fast reads (immutable)
    payment_method      VARCHAR(30),
    customer_name       VARCHAR(255),
    order_reference     TEXT,

    -- Per-transaction settlement status (mirrors parent but tracked separately)
    settlement_status   VARCHAR(30) NOT NULL DEFAULT 'pending'
                        CHECK (settlement_status IN (
                            'pending', 'processing', 'sent_to_bank',
                            'settled', 'failed', 'reversed'
                        )),

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Main lookup: all transactions for a settlement
CREATE INDEX IF NOT EXISTS idx_bst_settlement
    ON bittu_settlement_transactions(settlement_id, created_at DESC);

-- Per-restaurant transaction history (statement transaction list)
CREATE INDEX IF NOT EXISTS idx_bst_restaurant_created
    ON bittu_settlement_transactions(restaurant_id, created_at DESC);

-- Branch-scoped
CREATE INDEX IF NOT EXISTS idx_bst_branch_created
    ON bittu_settlement_transactions(restaurant_id, branch_id, created_at DESC);

-- Payment lookup (idempotency check)
CREATE INDEX IF NOT EXISTS idx_bst_payment
    ON bittu_settlement_transactions(payment_id)
    WHERE payment_id IS NOT NULL;

-- Order lookup
CREATE INDEX IF NOT EXISTS idx_bst_order
    ON bittu_settlement_transactions(order_id)
    WHERE order_id IS NOT NULL;

-- Status filter
CREATE INDEX IF NOT EXISTS idx_bst_status_restaurant
    ON bittu_settlement_transactions(restaurant_id, settlement_status, created_at DESC);

-- Prevent a payment from being settled twice
CREATE UNIQUE INDEX IF NOT EXISTS idx_bst_payment_unique
    ON bittu_settlement_transactions(payment_id, transaction_type)
    WHERE payment_id IS NOT NULL AND transaction_type = 'payment';


-- ════════════════════════════════════════════════════════════════════════════
-- 3. BITTU SETTLEMENT TIMELINE
--
--    Immutable audit trail for every status transition and system event.
--    Powers the "settlement timeline" card on the detail page.
--    Once inserted, rows are NEVER updated or deleted.
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS bittu_settlement_timeline (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    settlement_id   UUID NOT NULL REFERENCES bittu_settlements(id) ON DELETE CASCADE,
    restaurant_id   UUID NOT NULL,

    -- Event description
    event_type      VARCHAR(64) NOT NULL,
    -- e.g. 'created', 'processing_started', 'bank_transfer_initiated',
    --       'settled', 'failed', 'retried', 'reversed', 'note_added'

    title           VARCHAR(255) NOT NULL,
    description     TEXT,

    -- Previous → new status for transitions
    from_status     VARCHAR(30),
    to_status       VARCHAR(30),

    -- Who triggered this event
    actor_id        TEXT,           -- user_id or 'system'
    actor_type      VARCHAR(20) NOT NULL DEFAULT 'system'
                    CHECK (actor_type IN ('system', 'user', 'webhook')),

    -- Structured metadata (bank UTR, failure codes, etc.)
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Immutable timestamp
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Timeline ordered by settlement + time
CREATE INDEX IF NOT EXISTS idx_bst_timeline_settlement
    ON bittu_settlement_timeline(settlement_id, occurred_at DESC);

-- Prevent modification (trigger-based immutability)
CREATE OR REPLACE FUNCTION fn_prevent_timeline_update()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'settlement_timeline rows are immutable';
END;
$$;

DROP TRIGGER IF EXISTS trg_timeline_immutable ON bittu_settlement_timeline;
CREATE TRIGGER trg_timeline_immutable
    BEFORE UPDATE OR DELETE ON bittu_settlement_timeline
    FOR EACH ROW EXECUTE FUNCTION fn_prevent_timeline_update();


-- ════════════════════════════════════════════════════════════════════════════
-- 4. SETTLEMENT SUMMARY MATERIALIZED VIEW
--
--    Pre-aggregated per-restaurant/branch/date for fast dashboard queries.
--    Refreshed after each settlement status change via the service layer.
-- ════════════════════════════════════════════════════════════════════════════

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_settlement_daily_summary AS
SELECT
    bs.restaurant_id,
    bs.branch_id,
    DATE(bs.created_at)                                     AS summary_date,
    COUNT(*)                                                AS settlement_count,
    COUNT(*) FILTER (WHERE bs.settlement_status = 'settled')    AS settled_count,
    COUNT(*) FILTER (WHERE bs.settlement_status = 'pending')    AS pending_count,
    COUNT(*) FILTER (WHERE bs.settlement_status = 'failed')     AS failed_count,
    COALESCE(SUM(bs.gross_amount), 0)                       AS total_gross,
    COALESCE(SUM(bs.gross_amount) FILTER (WHERE bs.settlement_status = 'settled'), 0) AS total_settled,
    COALESCE(SUM(bs.gross_amount) FILTER (WHERE bs.settlement_status IN ('pending','processing','sent_to_bank')), 0) AS total_pending,
    COALESCE(SUM(bs.bittu_fee_amount + bs.gst_amount), 0)   AS total_deductions,
    COALESCE(SUM(bs.net_settlement_amount) FILTER (WHERE bs.settlement_status = 'settled'), 0) AS total_net_settled
FROM bittu_settlements bs
GROUP BY bs.restaurant_id, bs.branch_id, DATE(bs.created_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_settlement_daily_pk
    ON mv_settlement_daily_summary(
        restaurant_id,
        COALESCE(branch_id, '00000000-0000-0000-0000-000000000000'::uuid),
        summary_date
    );

CREATE INDEX IF NOT EXISTS idx_mv_settlement_daily_date
    ON mv_settlement_daily_summary(restaurant_id, summary_date DESC);


-- ════════════════════════════════════════════════════════════════════════════
-- 5. DAILY CLOSINGS INTEGRATION
--
--    Extend daily_closings to carry settlement summary data.
--    Allows daily close report to reflect pending/settled amounts.
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE daily_closings
    ADD COLUMN IF NOT EXISTS total_pending_settlement  NUMERIC(14,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_settled_today       NUMERIC(14,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_bittu_fees          NUMERIC(14,6) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_failed_settlements  NUMERIC(14,2) NOT NULL DEFAULT 0;


-- ════════════════════════════════════════════════════════════════════════════
-- 6. PERMISSIONS
--
--    Granular access control for the Statement module.
-- ════════════════════════════════════════════════════════════════════════════

INSERT INTO permissions (key) VALUES
    -- Read the statement dashboard & transactions
    ('statements.read'),
    -- Export PDF / Excel
    ('statements.export'),
    -- Admin: manually trigger retry, force status update (Bittu ops team)
    ('statements.admin'),
    -- View settlement detail including bank references
    ('statements.settlement.read')
ON CONFLICT (key) DO NOTHING;


-- ════════════════════════════════════════════════════════════════════════════
-- 7. SEED OWNER / MANAGER ROLE PERMISSIONS
--
--    Owners and managers can view & export statements by default.
--    Only admins can perform admin actions.
-- ════════════════════════════════════════════════════════════════════════════

-- Grant statements.read + statements.export + statements.settlement.read to
-- any existing 'owner' or 'manager' role across all branches.
DO $$
DECLARE
    v_perm_read     UUID;
    v_perm_export   UUID;
    v_perm_settle   UUID;
    v_role          RECORD;
BEGIN
    SELECT id INTO v_perm_read   FROM permissions WHERE key = 'statements.read';
    SELECT id INTO v_perm_export FROM permissions WHERE key = 'statements.export';
    SELECT id INTO v_perm_settle FROM permissions WHERE key = 'statements.settlement.read';

    FOR v_role IN
        SELECT id FROM roles WHERE lower(name) IN ('owner', 'manager')
    LOOP
        INSERT INTO role_permissions (role_id, permission_id, allowed)
            VALUES (v_role.id, v_perm_read,   true),
                   (v_role.id, v_perm_export, true),
                   (v_role.id, v_perm_settle, true)
        ON CONFLICT (role_id, permission_id) DO NOTHING;
    END LOOP;
END;
$$;


-- ════════════════════════════════════════════════════════════════════════════
-- 8. UPDATED_AT AUTO-TRIGGER for bittu_settlements
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_bittu_settlements_updated_at ON bittu_settlements;
CREATE TRIGGER trg_bittu_settlements_updated_at
    BEFORE UPDATE ON bittu_settlements
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();


COMMIT;
