-- Migration 026: Performance indexes for dine-in hot paths
-- Targets slow endpoints identified via profiling:
--   POST /tables/cart/add      (~3–7s)
--   GET  /dinein/sessions/{id}/bill  (~3s, polled every 5s)
--   POST /tables/sessions/{id}/payments (~6–7s)
--   GET  /tables                (~1.7–2.8s, polled)

-- ── 1. table_session_carts ──────────────────────────────────
-- Speeds up: cart lookups by session, item merge/upsert check in add_to_cart
CREATE INDEX IF NOT EXISTS idx_table_session_carts_session
    ON table_session_carts(session_id);

CREATE INDEX IF NOT EXISTS idx_table_session_carts_session_item
    ON table_session_carts(session_id, item_id);

-- ── 2. session_orders ───────────────────────────────────────
-- Speeds up: JOIN in get_session_bill  (WHERE so.session_id = $1)
CREATE INDEX IF NOT EXISTS idx_session_orders_session
    ON session_orders(session_id);

-- ── 3. order_items ──────────────────────────────────────────
-- Speeds up: bill aggregation  (WHERE oi.order_id = ANY($1::uuid[]))
CREATE INDEX IF NOT EXISTS idx_order_items_order
    ON order_items(order_id);

-- ── 4. restaurant_tables ────────────────────────────────────
-- Speeds up: list_tables  (WHERE user_id = $1 ORDER BY table_number)
CREATE INDEX IF NOT EXISTS idx_restaurant_tables_user_id
    ON restaurant_tables(user_id, table_number ASC);

-- ── 5. dine_in_sessions ─────────────────────────────────────
-- Speeds up: add_to_cart_admin compat lookup  (WHERE user_id = $1 AND status = 'active')
CREATE INDEX IF NOT EXISTS idx_dine_in_sessions_user_status
    ON dine_in_sessions(user_id, status);

-- ── 6. table_session_payments ───────────────────────────────
-- Supplementary covering index for totals aggregation in bill computation
-- (idx_table_session_payments_session on (session_id, created_at DESC) already exists
--  from migration 012; this partial index speeds up the SUM query specifically)
CREATE INDEX IF NOT EXISTS idx_table_session_payments_session_amount
    ON table_session_payments(session_id)
    INCLUDE (amount);
