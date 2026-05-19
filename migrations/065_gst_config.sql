-- ============================================================
-- 065 — Standardised GST / tax configuration
--
-- Adds explicit GST fields to restaurant_settings and per-row CGST/SGST
-- columns on orders so receipts/invoices can be reproduced exactly without
-- recomputing from rates at print time.
--
-- Backfill rules:
--   * tax_percentage > 0  -> gst_enabled = true,  cgst = sgst = tax_pct/2
--   * tax_percentage = 0  -> gst_enabled = false, all rates = 0
--   * gst_number is mirrored from restaurants.gst_number on first migrate
--   * existing orders get cgst_amount = sgst_amount = tax_amount/2
-- ============================================================

BEGIN;

-- ── restaurant_settings ──────────────────────────────────────
ALTER TABLE restaurant_settings
    ADD COLUMN IF NOT EXISTS gst_enabled        BOOLEAN       NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS gst_type           VARCHAR(20)   NOT NULL DEFAULT 'GST',
    ADD COLUMN IF NOT EXISTS gst_number         VARCHAR(20),
    ADD COLUMN IF NOT EXISTS gst_percentage     NUMERIC(5,2)  NOT NULL DEFAULT 5.00,
    ADD COLUMN IF NOT EXISTS cgst_percentage    NUMERIC(5,2)  NOT NULL DEFAULT 2.50,
    ADD COLUMN IF NOT EXISTS sgst_percentage    NUMERIC(5,2)  NOT NULL DEFAULT 2.50,
    ADD COLUMN IF NOT EXISTS tax_inclusive      BOOLEAN       NOT NULL DEFAULT false;

-- Backfill from existing tax_percentage (single rate) -> split 50/50.
UPDATE restaurant_settings rs
SET    gst_percentage  = COALESCE(rs.tax_percentage, 5)::numeric(5,2),
       cgst_percentage = (COALESCE(rs.tax_percentage, 5) / 2.0)::numeric(5,2),
       sgst_percentage = (COALESCE(rs.tax_percentage, 5) / 2.0)::numeric(5,2),
       gst_enabled     = (COALESCE(rs.tax_percentage, 0) > 0)
WHERE  rs.gst_percentage = 5.00  -- only rows still at the column default
   OR  rs.gst_percentage IS NULL;

-- Mirror restaurant.gst_number into settings where the merchant hasn't set
-- one explicitly. Falls back gracefully if restaurant_settings.restaurant_id
-- is NULL (legacy single-tenant rows keyed by user_id only).
UPDATE restaurant_settings rs
SET    gst_number = r.gst_number
FROM   restaurants r
WHERE  rs.restaurant_id = r.id
  AND  rs.gst_number IS NULL
  AND  r.gst_number  IS NOT NULL;

-- A merchant cannot be "GST enabled" without a GSTIN — silently downgrade
-- legacy rows where the number is missing, so the new validator on writes
-- doesn't strand existing data.
UPDATE restaurant_settings
SET    gst_enabled = false
WHERE  gst_enabled = true
  AND  (gst_number IS NULL OR length(trim(gst_number)) = 0);

-- Sanity constraint: CGST + SGST must equal GST percentage (within 0.01)
-- when GST is enabled. Soft-enforced — kept as CHECK with tolerance because
-- legacy rows may have rounding drift.
ALTER TABLE restaurant_settings
    DROP CONSTRAINT IF EXISTS restaurant_settings_gst_split_chk;
ALTER TABLE restaurant_settings
    ADD  CONSTRAINT restaurant_settings_gst_split_chk
    CHECK (
        gst_enabled = false
        OR ABS((cgst_percentage + sgst_percentage) - gst_percentage) <= 0.01
    );

ALTER TABLE restaurant_settings
    DROP CONSTRAINT IF EXISTS restaurant_settings_gst_range_chk;
ALTER TABLE restaurant_settings
    ADD  CONSTRAINT restaurant_settings_gst_range_chk
    CHECK (
        gst_percentage  BETWEEN 0 AND 28
        AND cgst_percentage BETWEEN 0 AND 28
        AND sgst_percentage BETWEEN 0 AND 28
    );

-- ── orders ───────────────────────────────────────────────────
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS cgst_amount   NUMERIC(12,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS sgst_amount   NUMERIC(12,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS gst_number    VARCHAR(20),
    ADD COLUMN IF NOT EXISTS round_off     NUMERIC(8,2)  NOT NULL DEFAULT 0;

-- Historical orders: assume intra-state, split tax_amount evenly.
UPDATE orders
SET    cgst_amount = ROUND(COALESCE(tax_amount, 0) / 2.0, 2),
       sgst_amount = COALESCE(tax_amount, 0) - ROUND(COALESCE(tax_amount, 0) / 2.0, 2)
WHERE  cgst_amount = 0
  AND  sgst_amount = 0
  AND  COALESCE(tax_amount, 0) > 0;

COMMIT;
