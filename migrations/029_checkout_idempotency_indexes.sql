-- ============================================================
-- Migration 029: Checkout Idempotency Table + Performance Indexes
-- Purpose:
--   1. checkout_idempotency — durable, auth-scoped record of every
--      committed checkout so retries replay the original response.
--   2. Performance indexes on orders for newest-first list queries.
-- Safe to re-run (all statements are IF NOT EXISTS / DO NOTHING).
-- ============================================================

-- ── 1. Checkout Idempotency Table ────────────────────────────
-- Unique constraint on (idempotency_key, user_id) ensures that even
-- two truly concurrent POST /checkout requests with the same key only
-- produce one order.  The response_payload column enables full replay
-- without re-fetching the order row.

CREATE TABLE IF NOT EXISTS checkout_idempotency (
    id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key  VARCHAR(255) NOT NULL,
    -- user_id is the OWNER's user_id (branch users stored under owner_id)
    -- so the scope is always the billing account, not the staff member.
    user_id          TEXT         NOT NULL,
    order_id         UUID         REFERENCES orders(id) ON DELETE CASCADE,
    -- Full JSON response stored here for zero-cost replay.
    response_payload JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- Keys expire after 24 h; a background sweep can delete expired rows.
    expires_at       TIMESTAMPTZ  NOT NULL DEFAULT (NOW() + INTERVAL '24 hours'),

    CONSTRAINT uq_checkout_idempotency_key_user
        UNIQUE (idempotency_key, user_id)
);

COMMENT ON TABLE checkout_idempotency IS
    'Durable idempotency store for POST /orders/checkout.
     Scoped to (idempotency_key, user_id) to prevent cross-tenant replay.
     response_payload stores the full committed response so retries never
     need to re-query the orders table.';

COMMENT ON COLUMN checkout_idempotency.response_payload IS
    'Complete JSON response returned to the client on the original request.
     Replayed verbatim (with idempotent=true added) on duplicate requests.';

-- ── 2. Expiry index — for periodic cleanup of expired rows ───
CREATE INDEX IF NOT EXISTS idx_checkout_idempotency_expires
    ON checkout_idempotency (expires_at);

-- ── 3. Orders list: primary sort index (tenant + newest-first) ─
-- Covers the most common query pattern:
--   WHERE user_id = $1  ORDER BY created_at DESC  LIMIT n
CREATE INDEX IF NOT EXISTS idx_orders_user_created_desc
    ON orders (user_id, created_at DESC);

-- ── 4. Orders list: branch-scoped sort index ─────────────────
-- Covers:  WHERE branch_id = $1 AND user_id = $2  ORDER BY created_at DESC
CREATE INDEX IF NOT EXISTS idx_orders_branch_created_desc
    ON orders (branch_id, created_at DESC)
    WHERE branch_id IS NOT NULL;

-- ── 5. Orders list: status filter (active orders only) ───────
CREATE INDEX IF NOT EXISTS idx_orders_user_status
    ON orders (user_id, status)
    WHERE status NOT IN ('Cancelled', 'Delivered', 'Served');

-- ── 6. order_items lookup by order_id ────────────────────────
-- Typically already exists, but guaranteed here.
CREATE INDEX IF NOT EXISTS idx_order_items_order_id
    ON order_items (order_id);
