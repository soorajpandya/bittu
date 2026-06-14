-- ============================================================================
-- 079_merchant_agreement_acceptances.sql
-- ----------------------------------------------------------------------------
-- Authoritative, append-only audit trail of Sub-Merchant Agreement
-- acceptances. The server is the source of truth: `accepted_at`, `ip`,
-- `user_id`, `restaurant_id` and the identity snapshot are stamped
-- server-side. Client-reported values are kept in separate columns
-- (`accepted_at_client`, `ip_client`, `user_agent`) for comparison.
--
-- WHY: Payment aggregators (Razorpay) require evidentiary proof of when a
-- merchant accepted the sub-merchant terms. A mutable record is not proof —
-- this table is append-only at the storage layer (UPDATE/DELETE rejected).
--
-- SAFE TO RE-RUN: idempotent (IF NOT EXISTS / CREATE OR REPLACE / DROP IF EXISTS).
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS merchant_agreement_acceptances (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID         NOT NULL,
    restaurant_id       UUID,
    agreement_type      TEXT         NOT NULL DEFAULT 'sub_merchant',
    version             TEXT         NOT NULL,
    agreement_sha256    TEXT         NOT NULL,
    -- ── Server-stamped, trust-critical ──
    accepted_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    ip                  INET,
    -- ── Identity snapshot at acceptance time ──
    user_agent          TEXT,
    name                TEXT,
    email               TEXT,
    business_name       TEXT,
    -- ── Client-reported (for comparison only; never authoritative) ──
    accepted_at_client  TIMESTAMPTZ,
    ip_client           TEXT,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_maa_user
    ON merchant_agreement_acceptances (user_id, accepted_at DESC);
CREATE INDEX IF NOT EXISTS ix_maa_restaurant
    ON merchant_agreement_acceptances (restaurant_id, accepted_at DESC);
CREATE INDEX IF NOT EXISTS ix_maa_type_version
    ON merchant_agreement_acceptances (agreement_type, version);

-- ── Append-only enforcement: reject any UPDATE/DELETE at the storage layer ──
CREATE OR REPLACE FUNCTION fn_maa_block_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'merchant_agreement_acceptances is append-only (% denied)', TG_OP;
END $$;

DROP TRIGGER IF EXISTS trg_maa_no_update ON merchant_agreement_acceptances;
DROP TRIGGER IF EXISTS trg_maa_no_delete ON merchant_agreement_acceptances;

CREATE TRIGGER trg_maa_no_update BEFORE UPDATE ON merchant_agreement_acceptances
    FOR EACH ROW EXECUTE FUNCTION fn_maa_block_mutation();
CREATE TRIGGER trg_maa_no_delete BEFORE DELETE ON merchant_agreement_acceptances
    FOR EACH ROW EXECUTE FUNCTION fn_maa_block_mutation();

COMMIT;
