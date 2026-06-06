-- ════════════════════════════════════════════════════════════════════════════
-- Migration 076 — Allow ON DELETE SET NULL on gst_invoice_items.item_id
-- ════════════════════════════════════════════════════════════════════════════
-- Problem:
--   `gst_invoice_items.item_id REFERENCES items("Item_ID") ON DELETE SET NULL`.
--   When a menu item is deleted, PostgreSQL fires a cascading UPDATE that sets
--   the affected gst_invoice_items rows' item_id to NULL. The immutability
--   trigger `trg_prevent_gst_invoice_update` (migration 007) blocks ALL
--   UPDATEs, so the parent DELETE fails with
--     RaiseError: gst_invoice_items are immutable. Create a credit note instead.
--   Symptom: DELETE /api/v1/items/{id} returns 500 for every item that was
--   ever billed on a GST invoice.
--
-- Fix:
--   Allow the single narrow case where the *only* change is item_id going
--   from NOT NULL to NULL (the FK SET NULL cascade) AND every other column
--   is unchanged. All other UPDATEs and all DELETEs remain blocked, so the
--   filed invoice content (amounts, tax, names, HSN, etc.) stays immutable.
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_prevent_gst_invoice_edit()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        -- Allow ONLY the FK ON DELETE SET NULL side-effect:
        --   item_id transitions from NOT NULL → NULL
        --   AND every other invoice-content column is unchanged.
        IF OLD.item_id IS NOT NULL
           AND NEW.item_id IS NULL
           AND NEW.invoice_id     IS NOT DISTINCT FROM OLD.invoice_id
           AND NEW.item_name      IS NOT DISTINCT FROM OLD.item_name
           AND NEW.hsn_code       IS NOT DISTINCT FROM OLD.hsn_code
           AND NEW.quantity       IS NOT DISTINCT FROM OLD.quantity
           AND NEW.unit_price     IS NOT DISTINCT FROM OLD.unit_price
           AND NEW.discount       IS NOT DISTINCT FROM OLD.discount
           AND NEW.taxable_value  IS NOT DISTINCT FROM OLD.taxable_value
           AND NEW.cgst_rate      IS NOT DISTINCT FROM OLD.cgst_rate
           AND NEW.cgst_amount    IS NOT DISTINCT FROM OLD.cgst_amount
           AND NEW.sgst_rate      IS NOT DISTINCT FROM OLD.sgst_rate
           AND NEW.sgst_amount    IS NOT DISTINCT FROM OLD.sgst_amount
           AND NEW.igst_rate      IS NOT DISTINCT FROM OLD.igst_rate
           AND NEW.igst_amount    IS NOT DISTINCT FROM OLD.igst_amount
           AND NEW.total_amount   IS NOT DISTINCT FROM OLD.total_amount
           AND NEW.created_at     IS NOT DISTINCT FROM OLD.created_at
        THEN
            RETURN NEW;
        END IF;
    END IF;

    RAISE EXCEPTION 'gst_invoice_items are immutable. Create a credit note instead.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
