-- ============================================================================
-- 080_merchant_subscriptions.sql
-- ----------------------------------------------------------------------------
-- Onboarding plan selection + Razorpay subscription tracking.
--
-- WHY: The onboarding wizard lets a merchant pick one of four plans
--   (starter | business | growth | enterprise). The two "Software" plans
--   (starter, business) bill a recurring SaaS subscription via Razorpay
--   Subscriptions; the merchant must complete that subscription payment
--   before they can proceed to restaurant-settings + KYC. The two
--   "Integrated Payments" plans (growth, enterprise) have a ₹0 subscription
--   (revenue is the per-transaction fee) so they have no upfront gate.
--
--   * restaurants.plan        — the merchant's selected plan (source of truth
--                               the FE reads back on session restore).
--   * merchant_subscriptions  — mirror of the Razorpay subscription object,
--                               kept in sync by the verify endpoint + the
--                               subscription.* webhooks. This is what the
--                               onboarding gate reads ("is the SaaS
--                               subscription authenticated/active?").
--   * merchant_subscription_events — append-only audit of every subscription
--                               webhook envelope for forensics/idempotency.
--
-- SAFE TO RE-RUN: idempotent (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS /
--   CREATE OR REPLACE / DROP IF EXISTS).
-- ============================================================================

BEGIN;

-- ── 1. Selected plan on the restaurant ─────────────────────────────────────
ALTER TABLE restaurants
    ADD COLUMN IF NOT EXISTS plan TEXT;

-- Constrain to the known plan ids (NULL = not yet chosen). Drop+recreate so
-- the migration is safe to re-run and easy to extend with new plan ids.
ALTER TABLE restaurants
    DROP CONSTRAINT IF EXISTS chk_restaurants_plan;
ALTER TABLE restaurants
    ADD CONSTRAINT chk_restaurants_plan
    CHECK (plan IS NULL OR plan IN ('starter', 'business', 'growth', 'enterprise'));

COMMENT ON COLUMN restaurants.plan IS
    'Onboarding plan: starter|business (recurring SaaS subscription) or growth|enterprise (integrated payments, ₹0 subscription). NULL ⇒ not chosen yet.';

-- ── 2. Razorpay subscription mirror ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS merchant_subscriptions (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id            UUID         NOT NULL,
    user_id                  TEXT,                       -- auth uid of the owner who created it
    plan                     TEXT         NOT NULL,      -- bittu plan id (starter|business|...)
    -- ── Razorpay identifiers ──
    razorpay_plan_id         TEXT         NOT NULL,      -- plan_XXX used to create the subscription
    razorpay_subscription_id TEXT         NOT NULL,      -- sub_XXX
    razorpay_customer_id     TEXT,
    -- ── Lifecycle ──
    status                   TEXT         NOT NULL DEFAULT 'created',
        -- created | authenticated | active | pending | halted | cancelled |
        -- completed | expired | paused  (mirrors Razorpay subscription.status)
    short_url                TEXT,                       -- hosted auth/checkout link
    total_count              INTEGER,
    paid_count               INTEGER      NOT NULL DEFAULT 0,
    remaining_count          INTEGER,
    charge_at                TIMESTAMPTZ,
    current_start            TIMESTAMPTZ,
    current_end              TIMESTAMPTZ,
    -- ── Trust-critical stamps ──
    authenticated_at         TIMESTAMPTZ,                -- first mandate/payment confirmed
    activated_at             TIMESTAMPTZ,
    cancelled_at             TIMESTAMPTZ,
    last_payment_id          TEXT,
    notes                    JSONB        NOT NULL DEFAULT '{}'::jsonb,
    raw                      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_merchant_subscriptions_rzp UNIQUE (razorpay_subscription_id)
);

CREATE INDEX IF NOT EXISTS ix_merchant_subscriptions_restaurant
    ON merchant_subscriptions (restaurant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_merchant_subscriptions_status
    ON merchant_subscriptions (status);

COMMENT ON TABLE merchant_subscriptions IS
    'Per-merchant Razorpay SaaS subscription mirror. Kept in sync by the verify endpoint and subscription.* webhooks. The onboarding gate treats status IN (authenticated, active) as "paid".';

-- ── 3. Append-only subscription webhook audit ──────────────────────────────
CREATE TABLE IF NOT EXISTS merchant_subscription_events (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    razorpay_subscription_id TEXT         NOT NULL,
    event                    TEXT         NOT NULL,
    razorpay_payment_id      TEXT,
    status                   TEXT,
    raw                      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_merchant_subscription_events_sub
    ON merchant_subscription_events (razorpay_subscription_id, created_at DESC);

COMMENT ON TABLE merchant_subscription_events IS
    'Append-only forensic log of every Razorpay subscription.* webhook envelope.';

COMMIT;
