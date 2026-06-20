-- ============================================================================
-- 081_merchant_device_orders.sql
-- ----------------------------------------------------------------------------
-- One-time POS device fee (₹30,000 + GST) for the business + enterprise
-- plans. This is billed SEPARATELY from the onboarding subscription gate:
-- it does NOT block the merchant from reaching restaurant-settings/KYC.
-- Collected as its own Razorpay order; the verify endpoint flips it to paid.
--
-- Applies to: business (gated subscription + device) and enterprise
-- (₹0 subscription, ungated, but still owes the device fee).
--
-- SAFE TO RE-RUN: idempotent (IF NOT EXISTS).
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS merchant_device_orders (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id       UUID         NOT NULL,
    user_id             TEXT,
    plan                TEXT         NOT NULL,
    -- ── Amounts (paise); total = amount + GST ──
    amount_paise        BIGINT       NOT NULL,
    gst_paise           BIGINT       NOT NULL DEFAULT 0,
    total_paise         BIGINT       NOT NULL,
    -- ── Razorpay order/payment ──
    razorpay_order_id   TEXT         NOT NULL,
    razorpay_payment_id TEXT,
    status              TEXT         NOT NULL DEFAULT 'created',
        -- created | paid | failed | cancelled
    paid_at             TIMESTAMPTZ,
    notes               JSONB        NOT NULL DEFAULT '{}'::jsonb,
    raw                 JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_merchant_device_orders_rzp UNIQUE (razorpay_order_id)
);

CREATE INDEX IF NOT EXISTS ix_merchant_device_orders_restaurant
    ON merchant_device_orders (restaurant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_merchant_device_orders_status
    ON merchant_device_orders (status);

COMMENT ON TABLE merchant_device_orders IS
    'One-time POS device fee orders (business/enterprise). Separate from the subscription gate; verified via the orders signature.';

COMMIT;
