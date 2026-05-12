-- 057: Drop Coupons, Deliveries (partner tracking), Offers, Feedback, Pincodes.
--
-- Code paths removed in same commit:
--   - app/api/v1/{coupons,offers,delivery,delivery_partners,feedback,pincodes}.py
--   - app/services/{coupon,offer,delivery,feedback,pincode}_service.py
--   - Permissions stripped from app/core/auth.py (delivery:*, coupons:*)
--
-- NOTE: orders.OrderStatus.OUT_FOR_DELIVERY/DELIVERED enum values are kept;
-- they describe the order's logical state, independent of partner tracking.

BEGIN;

-- Drop in dependency order (children before parents)
DROP TABLE IF EXISTS coupon_usage         CASCADE;
DROP TABLE IF EXISTS coupons              CASCADE;
DROP TABLE IF EXISTS offers               CASCADE;
DROP TABLE IF EXISTS delivery_tracking    CASCADE;
DROP TABLE IF EXISTS deliveries           CASCADE;
DROP TABLE IF EXISTS delivery_partners    CASCADE;
DROP TABLE IF EXISTS deliverable_pincodes CASCADE;
DROP TABLE IF EXISTS feedback             CASCADE;

-- Drop now-unused enums
DROP TYPE IF EXISTS delivery_status CASCADE;
DROP TYPE IF EXISTS partner_status  CASCADE;

COMMIT;
