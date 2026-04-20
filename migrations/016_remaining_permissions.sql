-- Migration 016: Add all remaining permission keys for full RBAC migration
-- Covers: menu, analytics, waitlist, dinein, customer, promotion, due_payment,
--         feedback, settings, purchase_order, audit, delivery, favourites
BEGIN;

-- New permission keys.
INSERT INTO permissions (key) VALUES
  ('menu.read'),
  ('menu.write'),
  ('menu.delete'),
  ('analytics.read'),
  ('waitlist.read'),
  ('waitlist.manage'),
  ('waitlist.admin'),
  ('dinein.manage'),
  ('customer.read'),
  ('customer.write'),
  ('customer.delete'),
  ('promotion.read'),
  ('promotion.write'),
  ('promotion.delete'),
  ('due_payment.read'),
  ('due_payment.write'),
  ('due_payment.delete'),
  ('feedback.read'),
  ('feedback.write'),
  ('feedback.delete'),
  ('settings.read'),
  ('settings.admin'),
  ('purchase_order.read'),
  ('purchase_order.write'),
  ('purchase_order.delete'),
  ('audit.read'),
  ('delivery.read'),
  ('delivery.write'),
  ('delivery.delete'),
  ('favourites.manage')
ON CONFLICT (key) DO NOTHING;

-- Assign role permissions.
WITH role_perm(role_name, perm_key, allowed, meta) AS (
  VALUES
  -- Owner: full access
  ('owner','menu.read',              true, '{}'::jsonb),
  ('owner','menu.write',             true, '{}'::jsonb),
  ('owner','menu.delete',            true, '{}'::jsonb),
  ('owner','analytics.read',         true, '{}'::jsonb),
  ('owner','waitlist.read',          true, '{}'::jsonb),
  ('owner','waitlist.manage',        true, '{}'::jsonb),
  ('owner','waitlist.admin',         true, '{}'::jsonb),
  ('owner','dinein.manage',          true, '{}'::jsonb),
  ('owner','customer.read',          true, '{}'::jsonb),
  ('owner','customer.write',         true, '{}'::jsonb),
  ('owner','customer.delete',        true, '{}'::jsonb),
  ('owner','promotion.read',         true, '{}'::jsonb),
  ('owner','promotion.write',        true, '{}'::jsonb),
  ('owner','promotion.delete',       true, '{}'::jsonb),
  ('owner','due_payment.read',       true, '{}'::jsonb),
  ('owner','due_payment.write',      true, '{}'::jsonb),
  ('owner','due_payment.delete',     true, '{}'::jsonb),
  ('owner','feedback.read',          true, '{}'::jsonb),
  ('owner','feedback.write',         true, '{}'::jsonb),
  ('owner','feedback.delete',        true, '{}'::jsonb),
  ('owner','settings.read',          true, '{}'::jsonb),
  ('owner','settings.admin',         true, '{}'::jsonb),
  ('owner','purchase_order.read',    true, '{}'::jsonb),
  ('owner','purchase_order.write',   true, '{}'::jsonb),
  ('owner','purchase_order.delete',  true, '{}'::jsonb),
  ('owner','audit.read',             true, '{}'::jsonb),
  ('owner','delivery.read',          true, '{}'::jsonb),
  ('owner','delivery.write',         true, '{}'::jsonb),
  ('owner','delivery.delete',        true, '{}'::jsonb),
  ('owner','favourites.manage',      true, '{}'::jsonb),

  -- Manager: read/write but no delete on most, plus analytics/waitlist admin
  ('manager','menu.read',            true, '{}'::jsonb),
  ('manager','menu.write',           true, '{}'::jsonb),
  ('manager','analytics.read',       true, '{}'::jsonb),
  ('manager','waitlist.read',        true, '{}'::jsonb),
  ('manager','waitlist.manage',      true, '{}'::jsonb),
  ('manager','waitlist.admin',       true, '{}'::jsonb),
  ('manager','dinein.manage',        true, '{}'::jsonb),
  ('manager','customer.read',        true, '{}'::jsonb),
  ('manager','customer.write',       true, '{}'::jsonb),
  ('manager','promotion.read',       true, '{}'::jsonb),
  ('manager','promotion.write',      true, '{}'::jsonb),
  ('manager','due_payment.read',     true, '{}'::jsonb),
  ('manager','due_payment.write',    true, '{}'::jsonb),
  ('manager','due_payment.delete',   true, '{}'::jsonb),
  ('manager','feedback.read',        true, '{}'::jsonb),
  ('manager','feedback.write',       true, '{}'::jsonb),
  ('manager','settings.read',        true, '{}'::jsonb),
  ('manager','purchase_order.read',  true, '{}'::jsonb),
  ('manager','purchase_order.write', true, '{}'::jsonb),
  ('manager','delivery.read',        true, '{}'::jsonb),
  ('manager','delivery.write',       true, '{}'::jsonb),
  ('manager','favourites.manage',    true, '{}'::jsonb),

  -- Cashier: menu read, customer CRUD, waitlist ops, due payments, feedback write
  ('cashier','menu.read',            true, '{}'::jsonb),
  ('cashier','waitlist.read',        true, '{}'::jsonb),
  ('cashier','waitlist.manage',      true, '{}'::jsonb),
  ('cashier','customer.read',        true, '{}'::jsonb),
  ('cashier','customer.write',       true, '{}'::jsonb),
  ('cashier','due_payment.read',     true, '{}'::jsonb),
  ('cashier','due_payment.write',    true, '{}'::jsonb),
  ('cashier','feedback.write',       true, '{}'::jsonb),
  ('cashier','favourites.manage',    true, '{}'::jsonb),

  -- Waiter: waitlist ops, dinein, feedback write, favourites
  ('waiter','waitlist.read',         true, '{}'::jsonb),
  ('waiter','waitlist.manage',       true, '{}'::jsonb),
  ('waiter','dinein.manage',         true, '{}'::jsonb),
  ('waiter','feedback.write',        true, '{}'::jsonb),
  ('waiter','favourites.manage',     true, '{}'::jsonb),

  -- Staff: waitlist read only
  ('staff','waitlist.read',          true, '{}'::jsonb)
)
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, rp.allowed, rp.meta
FROM role_perm rp
JOIN roles r ON r.name = rp.role_name
JOIN permissions p ON p.key = rp.perm_key
ON CONFLICT (role_id, permission_id) DO NOTHING;

COMMIT;
