-- 066: Per-line GST snapshot on order_items (item-level GST).
--
-- The store now has a global GST config (M065), but real-world restaurants
-- sell a mix of:
--
--   * Configurable items (restaurant-made food)
--       → GST EXCLUSIVE: tax is added on top of the selling price.
--
--   * MRP items (Coke, Thums Up, water bottles, packaged snacks)
--       → GST INCLUSIVE: tax is already baked into the printed MRP. The
--         customer must NOT be charged GST again.
--
--   * Non-GST items
--       → gst_enabled = false on the line.
--
-- M058 already added pricing_type/gst_rate/is_tax_inclusive on `items`.
-- This migration snapshots that decision per-line on `order_items` so
-- reprints stay byte-stable even if a merchant later edits the item.

BEGIN;

ALTER TABLE order_items
    ADD COLUMN IF NOT EXISTS gst_enabled    BOOLEAN       NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS gst_inclusive  BOOLEAN       NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS gst_rate       NUMERIC(5,2)  NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS taxable_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cgst_amount    NUMERIC(12,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS sgst_amount    NUMERIC(12,2) NOT NULL DEFAULT 0;

-- Backfill historical rows so reports don't see gaps. For pre-M066 orders
-- we infer:
--   * taxable_amount = total_price (no inclusive splits stored historically)
--   * cgst/sgst evenly split from the order-level tax_amount,
--     pro-rated by line share of subtotal.
UPDATE order_items oi
   SET taxable_amount = COALESCE(oi.total_price, 0),
       gst_rate       = CASE
           WHEN o.subtotal IS NULL OR o.subtotal = 0 THEN 0
           WHEN o.tax_amount IS NULL OR o.tax_amount = 0 THEN 0
           ELSE ROUND((o.tax_amount / o.subtotal) * 100, 2)
       END,
       gst_enabled    = (COALESCE(o.tax_amount, 0) > 0),
       gst_inclusive  = FALSE,
       cgst_amount    = CASE
           WHEN o.subtotal IS NULL OR o.subtotal = 0 THEN 0
           ELSE ROUND(
               (COALESCE(o.cgst_amount, o.tax_amount / 2)
                * oi.total_price / o.subtotal)::numeric, 2)
       END,
       sgst_amount    = CASE
           WHEN o.subtotal IS NULL OR o.subtotal = 0 THEN 0
           ELSE ROUND(
               (COALESCE(o.sgst_amount, o.tax_amount / 2)
                * oi.total_price / o.subtotal)::numeric, 2)
       END
  FROM orders o
 WHERE oi.order_id = o.id
   AND oi.cgst_amount = 0
   AND oi.sgst_amount = 0;

-- Range sanity (cheap CHECK; permissive so backfills don't trip).
ALTER TABLE order_items
    DROP CONSTRAINT IF EXISTS order_items_gst_rate_chk;
ALTER TABLE order_items
    ADD  CONSTRAINT order_items_gst_rate_chk
         CHECK (gst_rate >= 0 AND gst_rate <= 28);

COMMIT;
