-- ============================================================================
-- 043_refunds_disputes.sql — Phase 7: Refunds & Disputes
--
-- Tracks refund lifecycle and customer/bank disputes (chargebacks). Money
-- movement is recorded via fn_post_merchant_ledger_entry (Phase 1) using
-- transaction_type 'refund' / 'chargeback'. Audit trail is captured via
-- audit_events (Phase 6) at the service layer.
--
-- IMPORTANT: This module DOES NOT call any payment gateway. Refund initiation
-- is recorded internally; gateway integration is intentionally out of scope.
-- ============================================================================

BEGIN;

-- ── enums ───────────────────────────────────────────────────────────────────
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='refund_status_enum') THEN
        CREATE TYPE refund_status_enum AS ENUM (
            'initiated','processing','succeeded','failed','cancelled'
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='refund_kind_enum') THEN
        CREATE TYPE refund_kind_enum AS ENUM ('full','partial','goodwill');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='dispute_status_enum') THEN
        CREATE TYPE dispute_status_enum AS ENUM (
            'opened','under_review','evidence_submitted','won','lost','withdrawn'
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='dispute_kind_enum') THEN
        CREATE TYPE dispute_kind_enum AS ENUM (
            'chargeback','customer_complaint','fraud','service_issue','duplicate','other'
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='dispute_outcome_enum') THEN
        CREATE TYPE dispute_outcome_enum AS ENUM ('won','lost','withdrawn');
    END IF;
END $$;

-- ── refunds ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS refunds (
    id                  BIGSERIAL PRIMARY KEY,
    refund_uuid         UUID            NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    merchant_id         UUID            NOT NULL,
    payment_id          UUID            NOT NULL,
    order_id            UUID,
    amount              NUMERIC(18,4)   NOT NULL CHECK (amount > 0),
    currency            CHAR(3)         NOT NULL DEFAULT 'INR',
    kind                refund_kind_enum NOT NULL DEFAULT 'partial',
    status              refund_status_enum NOT NULL DEFAULT 'initiated',
    reason              TEXT,
    customer_contact    TEXT,
    gateway_refund_id   TEXT,
    initiated_by_user_id UUID,
    initiated_by_admin_id UUID,
    ledger_entry_id     UUID,
    notes               JSONB           NOT NULL DEFAULT '{}'::jsonb,
    failure_reason      TEXT,
    processed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),
    CONSTRAINT chk_refund_currency CHECK (currency = upper(currency))
);

CREATE INDEX IF NOT EXISTS idx_refunds_merchant_created
    ON refunds (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_refunds_payment ON refunds (payment_id);
CREATE INDEX IF NOT EXISTS idx_refunds_order   ON refunds (order_id) WHERE order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_refunds_status_created
    ON refunds (status, created_at DESC);

CREATE OR REPLACE FUNCTION fn_refunds_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_refunds_touch_updated_at ON refunds;
CREATE TRIGGER trg_refunds_touch_updated_at
    BEFORE UPDATE ON refunds
    FOR EACH ROW EXECUTE FUNCTION fn_refunds_touch_updated_at();

-- ── disputes ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS disputes (
    id                  BIGSERIAL PRIMARY KEY,
    dispute_uuid        UUID            NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    merchant_id         UUID            NOT NULL,
    payment_id          UUID,
    refund_id           BIGINT          REFERENCES refunds(id) ON DELETE SET NULL,
    order_id            UUID,
    kind                dispute_kind_enum NOT NULL,
    status              dispute_status_enum NOT NULL DEFAULT 'opened',
    amount              NUMERIC(18,4)   NOT NULL CHECK (amount > 0),
    currency            CHAR(3)         NOT NULL DEFAULT 'INR',
    customer_reference  TEXT,
    bank_case_id        TEXT,
    evidence            JSONB           NOT NULL DEFAULT '{}'::jsonb,
    notes               JSONB           NOT NULL DEFAULT '{}'::jsonb,
    opened_by_user_id   UUID,
    opened_by_admin_id  UUID,
    assigned_admin_id   UUID,
    outcome             dispute_outcome_enum,
    resolution_notes    TEXT,
    ledger_entry_id     UUID,
    due_at              TIMESTAMPTZ,
    opened_at           TIMESTAMPTZ     NOT NULL DEFAULT now(),
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),
    CONSTRAINT chk_dispute_currency CHECK (currency = upper(currency))
);

CREATE INDEX IF NOT EXISTS idx_disputes_merchant_created
    ON disputes (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_disputes_status_created
    ON disputes (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_disputes_payment ON disputes (payment_id) WHERE payment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_disputes_kind_status
    ON disputes (kind, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_disputes_due_at
    ON disputes (due_at) WHERE due_at IS NOT NULL AND status IN ('opened','under_review','evidence_submitted');

DROP TRIGGER IF EXISTS trg_disputes_touch_updated_at ON disputes;
CREATE TRIGGER trg_disputes_touch_updated_at
    BEFORE UPDATE ON disputes
    FOR EACH ROW EXECUTE FUNCTION fn_refunds_touch_updated_at();

-- ── dispute_events: append-only history of dispute state changes ────────────
CREATE TABLE IF NOT EXISTS dispute_events (
    id                  BIGSERIAL PRIMARY KEY,
    dispute_id          BIGINT          NOT NULL REFERENCES disputes(id) ON DELETE CASCADE,
    event_type          TEXT            NOT NULL, -- opened|status_changed|evidence_added|assigned|resolved|note
    from_status         dispute_status_enum,
    to_status           dispute_status_enum,
    payload             JSONB           NOT NULL DEFAULT '{}'::jsonb,
    actor_user_id       UUID,
    actor_admin_id      UUID,
    actor_label         TEXT,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dispute_events_dispute_created
    ON dispute_events (dispute_id, created_at DESC);

CREATE OR REPLACE FUNCTION fn_dispute_events_no_mutate()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'dispute_events is append-only (op=%)', TG_OP USING ERRCODE='P0002';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_dispute_events_no_update ON dispute_events;
CREATE TRIGGER trg_dispute_events_no_update
    BEFORE UPDATE ON dispute_events
    FOR EACH ROW EXECUTE FUNCTION fn_dispute_events_no_mutate();

DROP TRIGGER IF EXISTS trg_dispute_events_no_delete ON dispute_events;
CREATE TRIGGER trg_dispute_events_no_delete
    BEFORE DELETE ON dispute_events
    FOR EACH ROW EXECUTE FUNCTION fn_dispute_events_no_mutate();

-- ── helper: refundable amount remaining for a payment ───────────────────────
CREATE OR REPLACE FUNCTION fn_refundable_amount(
    p_merchant_id UUID,
    p_payment_id  UUID
) RETURNS NUMERIC AS $$
DECLARE
    v_paid     NUMERIC(18,4);
    v_refunded NUMERIC(18,4);
BEGIN
    SELECT COALESCE(amount, 0) INTO v_paid
      FROM payments
     WHERE id = p_payment_id
       AND (restaurant_id = p_merchant_id OR restaurant_id IS NULL);
    IF v_paid IS NULL THEN
        RETURN 0;
    END IF;

    SELECT COALESCE(SUM(amount), 0) INTO v_refunded
      FROM refunds
     WHERE merchant_id = p_merchant_id
       AND payment_id  = p_payment_id
       AND status IN ('initiated','processing','succeeded');

    RETURN GREATEST(v_paid - v_refunded, 0);
END;
$$ LANGUAGE plpgsql;

-- ── permissions ─────────────────────────────────────────────────────────────
INSERT INTO permissions (key) VALUES
    ('refunds.read'),
    ('refunds.write'),
    ('refunds.read.all'),
    ('refunds.write.all'),
    ('disputes.read'),
    ('disputes.write'),
    ('disputes.read.all'),
    ('disputes.write.all')
ON CONFLICT (key) DO NOTHING;

-- Owner: full merchant-side access. Manager: read+write.
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
  FROM roles r
  JOIN permissions p ON p.key IN (
        'refunds.read','refunds.write','disputes.read','disputes.write'
  )
 WHERE r.name IN ('owner','manager')
ON CONFLICT (role_id, permission_id) DO NOTHING;

COMMIT;
