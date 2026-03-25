-- ════════════════════════════════════════════════════════════════
-- BITTU Subscription System — Database Schema
-- Run this against your Supabase/PostgreSQL instance.
-- ════════════════════════════════════════════════════════════════

-- ── Subscription Plans ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS subscription_plans (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    slug            VARCHAR(50) UNIQUE NOT NULL,         -- starter, growth, pro
    description     TEXT DEFAULT '',
    price           NUMERIC(10,2) NOT NULL,              -- annual price incl. GST in INR
    monthly_price   NUMERIC(10,2),                       -- display-only monthly equivalent
    currency        VARCHAR(3) DEFAULT 'INR',
    interval        VARCHAR(20) DEFAULT 'yearly',        -- yearly | monthly
    features        JSONB DEFAULT '[]'::jsonb,           -- included features array
    limits          JSONB DEFAULT '[]'::jsonb,           -- plan limits array
    not_included    JSONB DEFAULT '[]'::jsonb,           -- features NOT in this plan
    highlight       BOOLEAN DEFAULT false,               -- "Most Popular" badge
    highlight_label VARCHAR(100),                        -- e.g. "Most restaurants choose this"
    cta_text        VARCHAR(100) DEFAULT 'Get Started',
    discount_label  VARCHAR(100),                        -- e.g. "Save ₹1500"
    razorpay_plan_id VARCHAR(100),                       -- Razorpay plan ID for recurring billing
    is_active       BOOLEAN DEFAULT true,
    sort_order      INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- ── User Subscriptions ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_subscriptions (
    id                       SERIAL PRIMARY KEY,
    user_id                  TEXT NOT NULL,
    plan_id                  INTEGER REFERENCES subscription_plans(id),
    status                   VARCHAR(30) DEFAULT 'TRIAL',
    -- TRIAL, ACTIVE, PAST_DUE, GRACE_PERIOD, SUSPENDED, CANCELLED, PENDING
    razorpay_subscription_id TEXT,
    razorpay_customer_id     TEXT,

    -- Trial
    trial_started_at         TIMESTAMPTZ,
    trial_expires_at         TIMESTAMPTZ,
    trial_end                TIMESTAMPTZ,
    trial_used               BOOLEAN DEFAULT false,

    -- Billing period
    current_period_start     TIMESTAMPTZ,
    current_period_end       TIMESTAMPTZ,
    next_billing_at          TIMESTAMPTZ,

    -- Payment tracking
    last_payment_at          TIMESTAMPTZ,
    payment_retry_count      INTEGER DEFAULT 0,
    grace_period_end         TIMESTAMPTZ,

    -- Lifecycle
    cancelled_at             TIMESTAMPTZ,
    ended_at                 TIMESTAMPTZ,
    upgrade_from_plan_id     INTEGER REFERENCES subscription_plans(id),
    downgrade_to_plan_id     INTEGER REFERENCES subscription_plans(id),
    downgrade_effective_at   TIMESTAMPTZ,
    created_at               TIMESTAMPTZ DEFAULT now(),
    updated_at               TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_user_subscriptions_user_id ON user_subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_user_subscriptions_status ON user_subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_user_subscriptions_razorpay ON user_subscriptions(razorpay_subscription_id);

-- ── Trial Eligibility (one trial per user) ──────────────────

CREATE TABLE IF NOT EXISTS trial_eligibility (
    user_id           TEXT PRIMARY KEY,
    trial_started_at  TIMESTAMPTZ NOT NULL,
    trial_expires_at  TIMESTAMPTZ NOT NULL
);

-- ── Billing History ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS billing_history (
    id                  SERIAL PRIMARY KEY,
    user_id             TEXT NOT NULL,
    subscription_id     INTEGER REFERENCES user_subscriptions(id),
    razorpay_payment_id TEXT,
    razorpay_order_id   TEXT,
    amount              NUMERIC(10,2) NOT NULL,
    currency            VARCHAR(3) DEFAULT 'INR',
    status              VARCHAR(30) DEFAULT 'pending',   -- pending, paid, failed, refunded
    description         TEXT DEFAULT '',
    paid_at             TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_billing_history_user ON billing_history(user_id);

-- ── Invoices ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS invoices (
    id                  SERIAL PRIMARY KEY,
    user_id             TEXT NOT NULL,
    subscription_id     INTEGER REFERENCES user_subscriptions(id),
    billing_history_id  INTEGER REFERENCES billing_history(id),
    invoice_number      TEXT UNIQUE NOT NULL,
    amount              NUMERIC(10,2) NOT NULL,
    tax_amount          NUMERIC(10,2) DEFAULT 0,
    total_amount        NUMERIC(10,2) NOT NULL,
    status              VARCHAR(30) DEFAULT 'draft',     -- draft, sent, paid
    issued_at           TIMESTAMPTZ DEFAULT now(),
    due_at              TIMESTAMPTZ,
    paid_at             TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- ── Add-on Orders (printer etc.) ────────────────────────────

CREATE TABLE IF NOT EXISTS addon_products (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(200) NOT NULL,
    slug        VARCHAR(50) UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    price       NUMERIC(10,2) NOT NULL,
    currency    VARCHAR(3) DEFAULT 'INR',
    image_url   TEXT,
    features    JSONB DEFAULT '[]'::jsonb,
    is_active   BOOLEAN DEFAULT true,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS addon_orders (
    id                   SERIAL PRIMARY KEY,
    user_id              TEXT NOT NULL,
    addon_id             INTEGER REFERENCES addon_products(id),
    quantity             INTEGER DEFAULT 1,
    amount               NUMERIC(10,2) NOT NULL,
    status               VARCHAR(30) DEFAULT 'pending',  -- pending, paid, shipped, delivered
    razorpay_order_id    TEXT,
    razorpay_payment_id  TEXT,
    shipping_address     JSONB,
    paid_at              TIMESTAMPTZ,
    created_at           TIMESTAMPTZ DEFAULT now(),
    updated_at           TIMESTAMPTZ DEFAULT now()
);

-- ── Seed Plans ──────────────────────────────────────────────

INSERT INTO subscription_plans (name, slug, price, monthly_price, description, interval, features, limits, not_included, highlight, highlight_label, cta_text, discount_label, sort_order)
VALUES
(
    'Starter', 'starter', 3538, 295,
    'Best for small setups',
    'yearly',
    '["Billing + KOT", "AI Menu Upload", "Voice Billing", "Table Management", "Basic Reports"]'::jsonb,
    '["Max 3 staff accounts", "Max 1 device login", "Single branch only", "Basic analytics only"]'::jsonb,
    '["WhatsApp Marketing", "Inventory Management", "Offers & Coupons", "Loyalty System"]'::jsonb,
    false, NULL, 'Start Free Trial', NULL, 1
),
(
    'Growth', 'growth', 7078, 590,
    'Most Popular — built for real business growth',
    'yearly',
    '["Everything in Starter", "WhatsApp Marketing", "Loyalty & Discounts", "Inventory Management", "Offers & Coupons", "Google Business Integration"]'::jsonb,
    '["Max 10 staff accounts", "Max 3 devices", "Single branch only", "Advanced analytics (limited depth)"]'::jsonb,
    '["Multi-branch support", "Audit logs", "Advanced analytics dashboard"]'::jsonb,
    true, 'Most restaurants choose this', 'Start My Setup', NULL, 2
),
(
    'Pro', 'pro', 11798, 983,
    'For scaling & multi-outlet businesses',
    'yearly',
    '["Everything in Growth", "Multi-branch Support", "Advanced Analytics Dashboard", "Audit Logs", "Role-Based Access Control"]'::jsonb,
    '["Max 25 staff accounts", "Max 5 devices"]'::jsonb,
    '[]'::jsonb,
    false, NULL, 'Scale My Business', NULL, 3
)
ON CONFLICT (slug) DO UPDATE SET
    price = EXCLUDED.price,
    monthly_price = EXCLUDED.monthly_price,
    description = EXCLUDED.description,
    features = EXCLUDED.features,
    limits = EXCLUDED.limits,
    not_included = EXCLUDED.not_included,
    highlight = EXCLUDED.highlight,
    highlight_label = EXCLUDED.highlight_label,
    cta_text = EXCLUDED.cta_text,
    discount_label = EXCLUDED.discount_label,
    updated_at = now();

-- ── Alter existing tables (safe to re-run) ─────────────────

ALTER TABLE subscription_plans ADD COLUMN IF NOT EXISTS limits JSONB DEFAULT '[]'::jsonb;
ALTER TABLE subscription_plans ADD COLUMN IF NOT EXISTS not_included JSONB DEFAULT '[]'::jsonb;
ALTER TABLE subscription_plans ADD COLUMN IF NOT EXISTS highlight_label VARCHAR(100);

-- ── Seed Add-on (Printer) ──────────────────────────────────

INSERT INTO addon_products (name, slug, price, description, features)
VALUES (
    'Thermal Printer', 'thermal-printer', 2999,
    '2-inch thermal receipt printer with 1-year warranty',
    '["2-inch thermal printer", "Bluetooth + USB", "1-year warranty", "Free shipping"]'::jsonb
)
ON CONFLICT (slug) DO UPDATE SET
    price = EXCLUDED.price,
    description = EXCLUDED.description,
    features = EXCLUDED.features;
