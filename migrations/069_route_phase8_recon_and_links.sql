-- 069_route_phase8_recon_and_links.sql
--
-- Phase 8 — close the enterprise traceability gaps so every paise is
-- auditable Payment → Order → Transfer → Settlement → Bank Credit.
--
-- 1. Denormalise rzp_route_transfers with restaurant_id, order_id (internal),
--    razorpay_order_id, recipient_settlement_id, refund_id and
--    reversal_of_transfer_id. These let the admin dashboard answer
--    "which transfer corresponds to this order" and "which settlement
--    is this transfer in" without 4-table joins.
--
-- 2. rzp_reconciliation_runs / rzp_reconciliation_discrepancies — minimal
--    reconciliation engine state. We already have a generic
--    `reconciliation_discrepancies` from migration 030 but it is bound
--    to bittu_settlements; this is a Razorpay-first 3-way matcher
--    table dedicated to Phase 9 reconciliation.

BEGIN;

-- ── 1. Transfer denormalisation ──────────────────────────────────────────
ALTER TABLE rzp_route_transfers
    ADD COLUMN IF NOT EXISTS restaurant_id            UUID,
    ADD COLUMN IF NOT EXISTS internal_order_id        UUID,
    ADD COLUMN IF NOT EXISTS razorpay_order_id        TEXT,
    ADD COLUMN IF NOT EXISTS recipient_settlement_id  TEXT,
    ADD COLUMN IF NOT EXISTS refund_id                TEXT,
    ADD COLUMN IF NOT EXISTS reversal_of_transfer_id  TEXT;

-- Settlement chain (transfer.processed populates recipient_settlement_id).
CREATE INDEX IF NOT EXISTS ix_rzp_transfers_settlement
    ON rzp_route_transfers (recipient_settlement_id)
    WHERE recipient_settlement_id IS NOT NULL;

-- Refund reversal chain.
CREATE INDEX IF NOT EXISTS ix_rzp_transfers_refund
    ON rzp_route_transfers (refund_id)
    WHERE refund_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_rzp_transfers_reversal_of
    ON rzp_route_transfers (reversal_of_transfer_id)
    WHERE reversal_of_transfer_id IS NOT NULL;

-- Restaurant + order chain.
CREATE INDEX IF NOT EXISTS ix_rzp_transfers_restaurant
    ON rzp_route_transfers (restaurant_id, created_at DESC)
    WHERE restaurant_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_rzp_transfers_internal_order
    ON rzp_route_transfers (internal_order_id)
    WHERE internal_order_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_rzp_transfers_rzp_order
    ON rzp_route_transfers (razorpay_order_id)
    WHERE razorpay_order_id IS NOT NULL;


-- ── 2. Reconciliation engine state ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS rzp_reconciliation_runs (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_completed_at      TIMESTAMPTZ,
    window_from           TIMESTAMPTZ NOT NULL,
    window_to             TIMESTAMPTZ NOT NULL,
    status                TEXT        NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed')),
    payments_scanned      INT         NOT NULL DEFAULT 0,
    transfers_scanned     INT         NOT NULL DEFAULT 0,
    settlements_scanned   INT         NOT NULL DEFAULT 0,
    discrepancies_found   INT         NOT NULL DEFAULT 0,
    error_message         TEXT,
    triggered_by          TEXT        NOT NULL DEFAULT 'scheduler',
    actor_user_id         UUID,
    metadata              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_rzp_recon_runs_started
    ON rzp_reconciliation_runs (run_started_at DESC);

CREATE INDEX IF NOT EXISTS ix_rzp_recon_runs_status
    ON rzp_reconciliation_runs (status, run_started_at DESC);


CREATE TABLE IF NOT EXISTS rzp_reconciliation_discrepancies (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id                UUID        NOT NULL REFERENCES rzp_reconciliation_runs(id) ON DELETE CASCADE,
    merchant_id           UUID,
    restaurant_id         UUID,

    -- discrepancy_type values (extend as needed):
    --   payment_without_transfer        captured payment, no Route transfer
    --   transfer_without_payment        transfer with razorpay_payment_id that has no rzp_payments row
    --   transfer_without_settlement     processed transfer, no recipient_settlement_id after T+3
    --   amount_mismatch_payment_transfer  amount_paise(transfer) != 0.95 * amount_paise(payment)
    --   refund_without_reversal         refund.processed but no reversed transfer in window
    --   reversal_without_refund         reversed transfer but no matching refund
    --   orphan_settlement               rzp_settlement with no transfers attributing to it
    discrepancy_type      TEXT        NOT NULL,
    severity              TEXT        NOT NULL DEFAULT 'medium'
        CHECK (severity IN ('low', 'medium', 'high', 'critical')),

    razorpay_payment_id   TEXT,
    transfer_id           TEXT,
    settlement_id         TEXT,
    refund_id             TEXT,

    expected_amount_paise BIGINT,
    actual_amount_paise   BIGINT,
    variance_paise        BIGINT,

    details               JSONB       NOT NULL DEFAULT '{}'::jsonb,
    status                TEXT        NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'investigating', 'resolved', 'ignored')),
    resolved_at           TIMESTAMPTZ,
    resolved_by_user_id   UUID,
    resolution_note       TEXT,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_rzp_recon_disc_run
    ON rzp_reconciliation_discrepancies (run_id);

CREATE INDEX IF NOT EXISTS ix_rzp_recon_disc_type_status
    ON rzp_reconciliation_discrepancies (discrepancy_type, status);

CREATE INDEX IF NOT EXISTS ix_rzp_recon_disc_merchant_open
    ON rzp_reconciliation_discrepancies (merchant_id, status)
    WHERE status = 'open';

CREATE INDEX IF NOT EXISTS ix_rzp_recon_disc_payment
    ON rzp_reconciliation_discrepancies (razorpay_payment_id)
    WHERE razorpay_payment_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_rzp_recon_disc_transfer
    ON rzp_reconciliation_discrepancies (transfer_id)
    WHERE transfer_id IS NOT NULL;

-- Dedupe key: one open discrepancy of a given type per linking id per run.
CREATE UNIQUE INDEX IF NOT EXISTS uq_rzp_recon_disc_open
    ON rzp_reconciliation_discrepancies (
        run_id, discrepancy_type,
        COALESCE(razorpay_payment_id, ''),
        COALESCE(transfer_id, ''),
        COALESCE(settlement_id, ''),
        COALESCE(refund_id, '')
    );

COMMIT;
