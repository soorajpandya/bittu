-- ════════════════════════════════════════════════════════════════════════════
-- 059_admin_ops_extensions.sql
--
-- Adds platform-ops surface area used by the Super Admin cockpit:
--   1. Operational suspension on `restaurants` (decoupled from KYC).
--   2. `merchant_admin_notes` — free-form internal notes thread per merchant.
--
-- All changes are additive and idempotent. No data is mutated.
-- ════════════════════════════════════════════════════════════════════════════

BEGIN;

-- ── 1. Operational suspension on restaurants ────────────────────────────────
ALTER TABLE restaurants
    ADD COLUMN IF NOT EXISTS suspended_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS suspended_reason  TEXT,
    ADD COLUMN IF NOT EXISTS suspended_by      UUID;

COMMENT ON COLUMN restaurants.suspended_at IS
  'Operational suspension timestamp set by a platform admin. NULL = active. '
  'Distinct from KYC compliance suspension (see merchant_kyc_profiles).';

CREATE INDEX IF NOT EXISTS ix_restaurants_suspended_at
    ON restaurants(suspended_at)
    WHERE suspended_at IS NOT NULL;


-- ── 2. Per-merchant internal notes thread ───────────────────────────────────
CREATE TABLE IF NOT EXISTS merchant_admin_notes (
    id              BIGSERIAL PRIMARY KEY,
    merchant_id     UUID        NOT NULL
                    REFERENCES restaurants(id) ON DELETE CASCADE,
    note            TEXT        NOT NULL CHECK (length(note) BETWEEN 1 AND 4000),
    author_id       UUID        NOT NULL,
    author_email    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_merchant_admin_notes_merchant
    ON merchant_admin_notes(merchant_id, created_at DESC);

COMMENT ON TABLE merchant_admin_notes IS
  'Internal-only notes recorded by platform admins against a merchant. '
  'Append-only at the application layer (no UPDATE endpoint). '
  'DELETE retained for compliance scrub if ever required.';

COMMIT;
