-- 068_route_stakeholder_and_product_config.sql
--
-- Extends rzp_route_accounts with the missing Razorpay Route onboarding
-- sub-steps:
--   3. Stakeholder         → stakeholder_id (sth_xxx) + raw payload
--   4. Request product cfg → route_product_id (acc_prd_xxx) + status + tnc_accepted_at
--   5. Update product cfg  → route_product_status flips to 'activated' on review
--
-- The full bank account number is NEVER persisted; only last4 + sha256(hash)
-- already exist on this table from migration 060.

BEGIN;

ALTER TABLE rzp_route_accounts
    ADD COLUMN IF NOT EXISTS stakeholder_id              TEXT,
    ADD COLUMN IF NOT EXISTS stakeholder_raw             JSONB,
    ADD COLUMN IF NOT EXISTS route_product_id            TEXT,
    ADD COLUMN IF NOT EXISTS route_product_status        TEXT,
    ADD COLUMN IF NOT EXISTS route_product_requested_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS route_product_activated_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS route_product_raw           JSONB,
    ADD COLUMN IF NOT EXISTS tnc_accepted_at             TIMESTAMPTZ;

-- Lookups by stakeholder / product id (webhook + admin dashboards).
CREATE UNIQUE INDEX IF NOT EXISTS uq_rzp_route_accounts_stakeholder_id
    ON rzp_route_accounts (stakeholder_id)
    WHERE stakeholder_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_rzp_route_accounts_route_product_id
    ON rzp_route_accounts (route_product_id)
    WHERE route_product_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_rzp_route_accounts_product_status
    ON rzp_route_accounts (route_product_status)
    WHERE route_product_status IS NOT NULL;

COMMIT;
