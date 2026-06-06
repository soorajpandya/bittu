-- Migration 078 — Static QR Payment Module
--
-- Adds dedicated tables for the Static QR Payment Module
-- (usage = multiple_use Razorpay QR codes, no order/checkout binding).
--
-- This module is ADDITIVE on top of the existing order-driven QR flow.
-- It does NOT modify or share tables with `rzp_qr_codes` / `rzp_orders`
-- to keep the route-settlement pipeline behaviour for order payments
-- completely unchanged.
--
-- Tables:
--   rzp_static_qr_codes     One active static QR per merchant (idempotent).
--   rzp_static_qr_payments  Razorpay-webhook-driven mirror of every
--                           payment scanned against a static QR.

BEGIN;

-- ── rzp_static_qr_codes ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rzp_static_qr_codes (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id           UUID         NOT NULL,
    linked_account_id     TEXT         NOT NULL,
    razorpay_qr_id        TEXT         NOT NULL UNIQUE,
    usage                 TEXT         NOT NULL DEFAULT 'multiple_use',
    fixed_amount          BOOLEAN      NOT NULL DEFAULT FALSE,
    status                TEXT         NOT NULL DEFAULT 'active',
    merchant_display_name TEXT         NOT NULL,
    original_qr_image_url TEXT,
    upi_intent            TEXT,
    bittu_qr_image        TEXT,
    notes                 JSONB        NOT NULL DEFAULT '{}'::jsonb,
    raw_response          JSONB,
    closed_at             TIMESTAMPTZ,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_rzp_static_qr_status
        CHECK (status IN ('active', 'closed'))
);

-- Only one ACTIVE static QR per merchant; closed rows can stack up for
-- audit history.
CREATE UNIQUE INDEX IF NOT EXISTS uq_rzp_static_qr_active_per_merchant
    ON rzp_static_qr_codes (merchant_id)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS ix_rzp_static_qr_merchant_created
    ON rzp_static_qr_codes (merchant_id, created_at DESC);


-- ── rzp_static_qr_payments ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rzp_static_qr_payments (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    razorpay_payment_id   TEXT         NOT NULL UNIQUE,
    razorpay_qr_id        TEXT         NOT NULL
        REFERENCES rzp_static_qr_codes (razorpay_qr_id) ON DELETE RESTRICT,
    merchant_id           UUID         NOT NULL,
    linked_account_id     TEXT,
    amount_paise          BIGINT       NOT NULL,
    currency              CHAR(3)      NOT NULL DEFAULT 'INR',
    status                TEXT         NOT NULL,
    payment_method        TEXT,
    vpa                   TEXT,
    payer_email           TEXT,
    payer_contact         TEXT,
    failure_code          TEXT,
    failure_reason        TEXT,
    raw_payload           JSONB,
    captured_at           TIMESTAMPTZ,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_rzp_static_qr_payment_status
        CHECK (status IN ('authorized', 'captured', 'failed', 'refunded'))
);

CREATE INDEX IF NOT EXISTS ix_rzp_static_qr_payments_merchant_created
    ON rzp_static_qr_payments (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_rzp_static_qr_payments_status_created
    ON rzp_static_qr_payments (status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_rzp_static_qr_payments_qr_created
    ON rzp_static_qr_payments (razorpay_qr_id, created_at DESC);

COMMIT;
