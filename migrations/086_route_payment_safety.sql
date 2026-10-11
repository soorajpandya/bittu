-- Fixed merchant entitlement and durable Route mutation records.
-- Existing payments are deliberately not repriced or backfilled.
BEGIN;
CREATE TABLE IF NOT EXISTS rzp_route_allocations (
    razorpay_payment_id text PRIMARY KEY,
    merchant_id uuid NOT NULL,
    gross_paise bigint NOT NULL CHECK (gross_paise > 0),
    merchant_paise bigint NOT NULL CHECK (merchant_paise >= 0 AND merchant_paise <= gross_paise),
    currency text NOT NULL CHECK (currency = 'INR'),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rzp_route_allocations_merchant ON rzp_route_allocations(merchant_id);
CREATE TABLE IF NOT EXISTS rzp_route_operations (
    operation_key text PRIMARY KEY,
    request_hash text NOT NULL,
    resource_key text NOT NULL,
    state text NOT NULL CHECK (state IN ('unknown', 'succeeded', 'rejected')),
    response jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS rzp_route_operations_unknown_resource
    ON rzp_route_operations(resource_key) WHERE state='unknown';
CREATE INDEX IF NOT EXISTS rzp_route_operations_resource ON rzp_route_operations(resource_key);
ALTER TABLE payment_webhook_events ADD COLUMN IF NOT EXISTS processing_token uuid;
ALTER TABLE payment_webhook_events ADD COLUMN IF NOT EXISTS processing_started_at timestamptz;
COMMIT;
