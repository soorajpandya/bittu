-- ════════════════════════════════════════════════════════════════
-- BITTU — QR Dine-In Session System v2
-- Session isolation, order merging, idempotency, lifecycle
-- ════════════════════════════════════════════════════════════════

-- ── Session Status Enum ─────────────────────────────────────
DO $$ BEGIN
    CREATE TYPE session_status AS ENUM ('active', 'completed', 'cancelled', 'expired', 'merged');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ── Order Status: add ACTIVE for editable dine-in orders ────
-- (orders.status is plain text, no enum to alter)

-- ── Dine-In Sessions (replaces table_sessions for QR flow) ──
CREATE TABLE IF NOT EXISTS dine_in_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    table_id        UUID NOT NULL REFERENCES restaurant_tables(id),
    restaurant_id   UUID NOT NULL,
    user_id         TEXT NOT NULL,              -- owner_id
    branch_id       UUID,
    session_token   TEXT NOT NULL UNIQUE,       -- crypto random, sent to client
    device_id       TEXT,                       -- device that created the session
    guest_count     INTEGER DEFAULT 1,
    status          session_status DEFAULT 'active',
    active_order_id UUID,                       -- FK to orders.id, the one editable order
    merged_into_session_id UUID,                -- if merged, points to target session
    last_activity_at TIMESTAMPTZ DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now(),
    ended_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dinein_sessions_table ON dine_in_sessions(table_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_dinein_sessions_token ON dine_in_sessions(session_token);
CREATE INDEX IF NOT EXISTS idx_dinein_sessions_order ON dine_in_sessions(active_order_id);
CREATE INDEX IF NOT EXISTS idx_dinein_sessions_merged ON dine_in_sessions(merged_into_session_id) WHERE merged_into_session_id IS NOT NULL;

-- ── Session-Order Link (many sessions → one order after merge) ──
CREATE TABLE IF NOT EXISTS session_orders (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID NOT NULL REFERENCES dine_in_sessions(id),
    order_id        UUID NOT NULL,              -- FK to orders.id
    role            TEXT DEFAULT 'owner',       -- 'owner' or 'linked' (via merge)
    linked_at       TIMESTAMPTZ DEFAULT now(),
    UNIQUE (session_id, order_id)
);

CREATE INDEX IF NOT EXISTS idx_session_orders_session ON session_orders(session_id);
CREATE INDEX IF NOT EXISTS idx_session_orders_order ON session_orders(order_id);

-- ── Idempotency Keys for Cart/Order Actions ─────────────────
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key             TEXT PRIMARY KEY,
    session_id      UUID NOT NULL,
    result          JSONB,
    created_at      TIMESTAMPTZ DEFAULT now(),
    expires_at      TIMESTAMPTZ DEFAULT now() + INTERVAL '24 hours'
);

CREATE INDEX IF NOT EXISTS idx_idempotency_session ON idempotency_keys(session_id);
CREATE INDEX IF NOT EXISTS idx_idempotency_expires ON idempotency_keys(expires_at);

-- ── Session Cart (enhanced with idempotency) ────────────────
-- Reuses existing table_session_carts but add request_id column if missing
DO $$
BEGIN
    ALTER TABLE table_session_carts ADD COLUMN IF NOT EXISTS request_id TEXT;
EXCEPTION WHEN undefined_table THEN NULL;
END $$;

-- ── Order Items: add session_id for traceability ────────────
DO $$
BEGIN
    ALTER TABLE order_items ADD COLUMN IF NOT EXISTS session_id UUID;
EXCEPTION WHEN undefined_table THEN NULL;
END $$;

-- ── Kitchen Orders: add session grouping ────────────────────
-- table_session_id already exists in kitchen_orders (used by existing code)

-- ── Cleanup: auto-expire old idempotency keys ──────────────
-- (Run periodically via cron or pg_cron)
-- DELETE FROM idempotency_keys WHERE expires_at < now();

-- ── Function: Touch session activity ────────────────────────
CREATE OR REPLACE FUNCTION touch_session_activity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.last_activity_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_touch_session ON dine_in_sessions;
CREATE TRIGGER trg_touch_session
    BEFORE UPDATE ON dine_in_sessions
    FOR EACH ROW EXECUTE FUNCTION touch_session_activity();
