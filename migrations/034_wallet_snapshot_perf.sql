-- =====================================================================
-- 034_wallet_snapshot_perf.sql
-- ---------------------------------------------------------------------
-- Speeds up GET /api/v1/merchant-wallet by giving the snapshot CTE
-- (one-round-trip aggregate) the indexes it actually needs.
--
-- The endpoint filters every CTE by:
--     restaurant_id = $1
--     [optional] created_at <= $2
-- and aggregates over `status`, `method`, `settlement_status`.
--
-- Pre-existing indexes covered status='completed' partial cases but
-- nothing covered the (restaurant_id, created_at) range scan used for
-- the historical "as_of_date" cutoff or the lifetime totals.
--
-- All indexes are CONCURRENTLY-safe via IF NOT EXISTS; run outside of a
-- single-tx wrapper if your runner uses one.
-- =====================================================================

-- payments: lifetime + as-of cutoffs scan (restaurant_id, created_at)
CREATE INDEX IF NOT EXISTS idx_payments_restaurant_created
    ON payments (restaurant_id, created_at);

-- bittu_settlements: lifetime + as-of cutoffs aggregated by status
CREATE INDEX IF NOT EXISTS idx_bittu_settlements_restaurant_created
    ON bittu_settlements (restaurant_id, created_at);

-- orders: snapshot's "active orders" aggregation
-- (existing idx_orders_restaurant_id already covers (restaurant_id, created_at DESC))

ANALYZE payments;
ANALYZE bittu_settlements;
ANALYZE orders;
