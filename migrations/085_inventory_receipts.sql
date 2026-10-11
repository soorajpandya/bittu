-- Apply before deploying the inventory receipt API.
BEGIN;
ALTER TABLE purchase_order_items
    ADD COLUMN IF NOT EXISTS quantity_received NUMERIC NOT NULL DEFAULT 0
    CHECK (quantity_received >= 0);
-- Preserve already completed purchase orders; do not recreate historical stock.
UPDATE purchase_order_items i
   SET quantity_received = COALESCE(i.quantity_ordered, i.quantity, 0)
  FROM purchase_orders p
 WHERE p.id = i.purchase_order_id AND p.status = 'received'
   AND i.quantity_received = 0;
COMMIT;
