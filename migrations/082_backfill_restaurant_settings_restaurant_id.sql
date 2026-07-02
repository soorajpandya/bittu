-- Link legacy owner-keyed restaurant settings rows to their restaurant.
--
-- Checkout resolves GST configuration by restaurant_id, while older rows
-- were created with only user_id. Those rows were visible in Settings but
-- invisible to checkout, causing the tax engine to fall back to 5% GST.

BEGIN;

UPDATE restaurant_settings rs
SET    restaurant_id = r.id,
       updated_at = NOW()
FROM   restaurants r
WHERE  rs.restaurant_id IS NULL
  AND  rs.user_id::text = r.owner_id::text;

COMMIT;

