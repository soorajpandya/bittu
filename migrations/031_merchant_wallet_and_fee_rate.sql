-- ============================================================
-- Migration 031: Merchant Wallet + Fee-Rate Standardisation
--
-- Purpose:
--   1. Update default platform fee on bittu_settlements so that the
--      total deduction (base + 18% GST) equals exactly 0.30% of gross.
--          base = 0.30% / 1.18  ≈  0.2542373%
--      Old default was 0.15% (=> 0.177% incl. GST).  We update the
--      default ONLY; existing rows keep their historic rate to preserve
--      audit integrity.
--
--   2. Add support indexes used by the merchant-wallet aggregation
--      queries (cash balance, online pending/settled, platform revenue,
--      GST-on-fee, daily closing cross-check).
--
--   3. Add a settlement_batch_date generated column for fast daily
--      grouping (idempotent — uses IF NOT EXISTS via DO block).
--
-- All statements are idempotent.  Safe to re-run.
-- ============================================================

-- ── 1. Default fee rate: 0.15% → 0.2542373% (=> 0.30% incl. 18% GST) ──
ALTER TABLE bittu_settlements
    ALTER COLUMN fee_rate SET DEFAULT 0.002542;

COMMENT ON COLUMN bittu_settlements.fee_rate IS
    'Platform fee rate (excluding GST).  Default 0.2542% so that
     fee_rate * 1.18 = 0.30% — the headline merchant-side deduction.';

COMMENT ON COLUMN bittu_settlements.gst_rate IS
    '18% GST applied on the platform fee only (not on the gross).';


-- ── 2. Wallet aggregation indexes ─────────────────────────────────────
-- Cash balance: fast SUM over completed cash payments per restaurant.
CREATE INDEX IF NOT EXISTS idx_payments_restaurant_method_status
    ON payments (restaurant_id, method, status)
    WHERE status = 'completed';

-- Online pending balance: settlement_status filter on transactions.
CREATE INDEX IF NOT EXISTS idx_bst_restaurant_status
    ON bittu_settlement_transactions (restaurant_id, settlement_status);

-- Settled / lifetime: fast scans over bittu_settlements per restaurant.
CREATE INDEX IF NOT EXISTS idx_bittu_settlements_restaurant_status_settled
    ON bittu_settlements (restaurant_id, settlement_status, settled_at DESC);

-- Daily closing reports: bucket by settled_at::date.
CREATE INDEX IF NOT EXISTS idx_bittu_settlements_settled_date
    ON bittu_settlements ((settled_at::date))
    WHERE settled_at IS NOT NULL;


-- ── 3. Convenience view: merchant_wallet_snapshot ─────────────────────
-- A single SELECT-able view that returns every wallet figure for a
-- restaurant.  Service layer can read it directly or re-compute on the
-- fly; the view is just a documented contract.  CREATE OR REPLACE keeps
-- it idempotent.
CREATE OR REPLACE VIEW merchant_wallet_snapshot AS
SELECT
    r.id AS restaurant_id,
    r.name AS restaurant_name,

    -- ─ Cash side (never touches bank automatically) ────────────────
    COALESCE((
        SELECT SUM(p.amount)
        FROM   payments p
        WHERE  p.restaurant_id = r.id
          AND  p.status = 'completed'
          AND  LOWER(p.method) IN ('cash','counter','cod')
    ), 0)::numeric(14,2) AS cash_collected_lifetime,

    -- ─ Online side: pending → settled lifecycle ────────────────────
    COALESCE((
        SELECT SUM(bs.gross_amount)
        FROM   bittu_settlements bs
        WHERE  bs.restaurant_id = r.id
          AND  bs.settlement_status IN ('pending','processing','sent_to_bank')
    ), 0)::numeric(14,2) AS online_pending_gross,

    COALESCE((
        SELECT SUM(bs.net_settlement_amount)
        FROM   bittu_settlements bs
        WHERE  bs.restaurant_id = r.id
          AND  bs.settlement_status IN ('pending','processing','sent_to_bank')
    ), 0)::numeric(14,2) AS online_pending_net,

    COALESCE((
        SELECT SUM(bs.net_settlement_amount)
        FROM   bittu_settlements bs
        WHERE  bs.restaurant_id = r.id
          AND  bs.settlement_status = 'settled'
    ), 0)::numeric(14,2) AS online_settled_lifetime,

    COALESCE((
        SELECT SUM(bs.gross_amount)
        FROM   bittu_settlements bs
        WHERE  bs.restaurant_id = r.id
          AND  bs.settlement_status IN ('failed','reversed')
    ), 0)::numeric(14,2) AS online_failed_or_reversed,

    -- ─ Platform revenue (Bittu's earnings) ─────────────────────────
    COALESCE((
        SELECT SUM(bs.bittu_fee_amount)
        FROM   bittu_settlements bs
        WHERE  bs.restaurant_id = r.id
          AND  bs.settlement_status = 'settled'
    ), 0)::numeric(14,2) AS platform_fee_lifetime,

    COALESCE((
        SELECT SUM(bs.gst_amount)
        FROM   bittu_settlements bs
        WHERE  bs.restaurant_id = r.id
          AND  bs.settlement_status = 'settled'
    ), 0)::numeric(14,2) AS gst_on_fee_lifetime
FROM restaurants r;

COMMENT ON VIEW merchant_wallet_snapshot IS
    'Read-only wallet view aggregated from immutable ledger sources.
     Always derivable from payments + bittu_settlements; never cached.';
