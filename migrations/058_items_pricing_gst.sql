-- 058: Items pricing model (MRP vs configurable) + GST fields.
--
-- Adds the pricing/GST taxonomy expected by the frontend item form and POS
-- billing engine:
--
--   pricing_type     : 'mrp'          → packaged goods, GST baked into MRP
--                      'configurable' → restaurant-prepared, GST applied on top
--   gst_rate         : applicable GST slab in percent (0/5/12/18/28)
--   is_tax_inclusive : convenience boolean (true ⇔ pricing_type='mrp')
--   price_before_tax : pre-GST base (required for 'configurable')
--   final_price      : GST-inclusive total (canonical billing amount)
--
-- The legacy `price` column is preserved with this semantic:
--   * mrp          → price = MRP                = final_price
--   * configurable → price = price_before_tax   ≠ final_price
-- This keeps form round-trips idempotent (no compounding GST).

BEGIN;

ALTER TABLE items
    ADD COLUMN IF NOT EXISTS pricing_type     TEXT
        NOT NULL DEFAULT 'configurable',
    ADD COLUMN IF NOT EXISTS gst_rate         NUMERIC(5,2)
        NOT NULL DEFAULT 5,
    ADD COLUMN IF NOT EXISTS is_tax_inclusive BOOLEAN
        NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS price_before_tax NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS final_price      NUMERIC(10,2);

-- Drop & recreate constraints idempotently
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'items_pricing_type_check'
    ) THEN
        ALTER TABLE items DROP CONSTRAINT items_pricing_type_check;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'items_gst_rate_check'
    ) THEN
        ALTER TABLE items DROP CONSTRAINT items_gst_rate_check;
    END IF;
END $$;

ALTER TABLE items
    ADD CONSTRAINT items_pricing_type_check
        CHECK (pricing_type IN ('mrp','configurable')),
    ADD CONSTRAINT items_gst_rate_check
        CHECK (gst_rate IN (0, 5, 12, 18, 28));

-- Backfill: every legacy row is treated as configurable @ 5% GST and the
-- existing `price` is used as both base and final (operator can re-edit later).
UPDATE items
   SET pricing_type     = COALESCE(pricing_type,     'configurable'),
       is_tax_inclusive = COALESCE(is_tax_inclusive, FALSE),
       gst_rate         = COALESCE(gst_rate,         5),
       price_before_tax = COALESCE(price_before_tax, price),
       final_price      = COALESCE(final_price,      price)
 WHERE final_price IS NULL
    OR price_before_tax IS NULL;

COMMIT;
