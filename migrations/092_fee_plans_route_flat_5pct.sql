-- =====================================================================
-- Migration 092 — Fee plans: retire the 0.30% legacy default simulation
--                 plan and seed the live Route policy (flat 5%, 18% GST)
-- =====================================================================
-- Fee plans remain pricing_mode=simulation (they never change capture
-- pricing); this only makes the default simulation match reality.
-- Idempotent.
-- =====================================================================
BEGIN;

UPDATE fee_plans
   SET is_active = false, is_default = false, valid_to = COALESCE(valid_to, now()),
       description = COALESCE(description, '') || ' [retired by migration 092: superseded by route_flat_5pct]'
 WHERE code = 'default_v1' AND (is_active OR is_default);

INSERT INTO fee_plans (code, name, description, gst_rate, is_default, is_active, metadata)
VALUES (
  'route_flat_5pct',
  'Razorpay Route — flat 5%',
  'Live policy: 5% total deduction on online payments (4.2373% platform fee + 18% GST on the fee, full-consideration basis). Merchant receives 95%. Cash/direct POS collections carry no deduction.',
  0.180000, true, true,
  '{"pricing_mode":"simulation","applies_to_route":false,"fee_basis":"full_consideration","mirrors_live_policy":true}'::jsonb
) ON CONFLICT (code) DO UPDATE
  SET is_default = true, is_active = true, gst_rate = 0.180000,
      name = EXCLUDED.name, description = EXCLUDED.description, metadata = EXCLUDED.metadata;

-- Wildcard 5% all-in.
INSERT INTO fee_plan_rules
  (plan_id, payment_method, order_source, min_amount, max_amount, fee_type, percent_rate, flat_fee, priority, is_active)
SELECT p.id, NULL, NULL, 0, NULL, 'percent', 0.050000, 0, 0, true
  FROM fee_plans p WHERE p.code = 'route_flat_5pct'
   AND NOT EXISTS (SELECT 1 FROM fee_plan_rules r WHERE r.plan_id = p.id AND r.payment_method IS NULL);

-- Zero fee for cash-equivalent and direct POS methods.
INSERT INTO fee_plan_rules
  (plan_id, payment_method, order_source, min_amount, max_amount, fee_type, percent_rate, flat_fee, priority, is_active)
SELECT p.id, m.method, NULL, 0, NULL, 'percent', 0, 0, 100, true
  FROM fee_plans p
  CROSS JOIN (VALUES ('cash'), ('counter'), ('cod')) AS m(method)
 WHERE p.code = 'route_flat_5pct'
   AND NOT EXISTS (SELECT 1 FROM fee_plan_rules r WHERE r.plan_id = p.id AND r.payment_method = m.method);

COMMIT;
