-- RBAC system: roles, permissions, role_permissions, activity_logs
-- Safe to run multiple times.

BEGIN;

CREATE TABLE IF NOT EXISTS roles (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name VARCHAR(64) NOT NULL,
  branch_id UUID NOT NULL REFERENCES sub_branches(id) ON DELETE CASCADE,
  is_default BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(branch_id, name)
);

CREATE TABLE IF NOT EXISTS permissions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  key VARCHAR(128) NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS role_permissions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  permission_id UUID NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
  allowed BOOLEAN NOT NULL DEFAULT true,
  meta JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(role_id, permission_id)
);

ALTER TABLE branch_users
  ADD COLUMN IF NOT EXISTS role_id UUID REFERENCES roles(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS activity_logs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id TEXT NOT NULL,
  branch_id UUID NULL REFERENCES sub_branches(id) ON DELETE SET NULL,
  action VARCHAR(128) NOT NULL,
  entity_type VARCHAR(64) NOT NULL,
  entity_id UUID NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_activity_logs_user_created_at ON activity_logs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_logs_branch_created_at ON activity_logs(branch_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_role_permissions_role ON role_permissions(role_id);
CREATE INDEX IF NOT EXISTS idx_role_permissions_permission ON role_permissions(permission_id);

-- Seed permission keys (includes existing and new action naming for compatibility).
INSERT INTO permissions (key) VALUES
  ('order.create'),
  ('order.edit'),
  ('order.cancel'),
  ('order.read'),
  ('orders.create'),
  ('orders.read'),
  ('orders.update'),
  ('billing.generate'),
  ('billing.discount'),
  ('payment.create'),
  ('payment.refund'),
  ('payments.create'),
  ('payments.refund'),
  ('table.read'),
  ('table.start'),
  ('table.close'),
  ('table.manage'),
  ('tables.manage'),
  ('inventory.read'),
  ('inventory.update'),
  ('inventory.manage'),
  ('voice.use'),
  ('kitchen.read'),
  ('kitchen.update'),
  ('kitchen_station.read'),
  ('kitchen_station.manage')
ON CONFLICT (key) DO NOTHING;

-- Seed standard roles per branch.
INSERT INTO roles (name, branch_id, is_default)
SELECT role_name, sb.id, role_name IN ('owner','manager','cashier','waiter','kitchen','staff')
FROM sub_branches sb
CROSS JOIN (VALUES ('owner'), ('manager'), ('cashier'), ('waiter'), ('kitchen'), ('staff')) AS r(role_name)
ON CONFLICT (branch_id, name) DO NOTHING;

-- Branch users role_id backfill using existing role text.
UPDATE branch_users bu
SET role_id = r.id
FROM roles r
WHERE bu.role_id IS NULL
  AND r.branch_id = bu.branch_id
  AND (
    lower(r.name) = lower(bu.role)
    OR (lower(bu.role) = 'chef' AND lower(r.name) = 'kitchen')
  );

-- Assign role permissions by role name.
WITH role_perm(role_name, perm_key, allowed, meta) AS (
  VALUES
  -- Owner
  ('owner','order.create', true, '{}'::jsonb),
  ('owner','order.edit', true, '{}'::jsonb),
  ('owner','order.cancel', true, '{}'::jsonb),
  ('owner','order.read', true, '{}'::jsonb),
  ('owner','orders.create', true, '{}'::jsonb),
  ('owner','orders.read', true, '{}'::jsonb),
  ('owner','orders.update', true, '{}'::jsonb),
  ('owner','billing.generate', true, '{}'::jsonb),
  ('owner','billing.discount', true, '{"max_discount_percent": 100}'::jsonb),
  ('owner','payment.create', true, '{}'::jsonb),
  ('owner','payment.refund', true, '{}'::jsonb),
  ('owner','payments.create', true, '{}'::jsonb),
  ('owner','payments.refund', true, '{}'::jsonb),
  ('owner','table.read', true, '{}'::jsonb),
  ('owner','table.start', true, '{}'::jsonb),
  ('owner','table.close', true, '{}'::jsonb),
  ('owner','table.manage', true, '{}'::jsonb),
  ('owner','tables.manage', true, '{}'::jsonb),
  ('owner','inventory.read', true, '{}'::jsonb),
  ('owner','inventory.update', true, '{}'::jsonb),
  ('owner','inventory.manage', true, '{}'::jsonb),
  ('owner','voice.use', true, '{}'::jsonb),
  ('owner','kitchen.read', true, '{}'::jsonb),
  ('owner','kitchen.update', true, '{}'::jsonb),
  ('owner','kitchen_station.read', true, '{}'::jsonb),
  ('owner','kitchen_station.manage', true, '{}'::jsonb),
  -- Manager
  ('manager','order.create', true, '{}'::jsonb),
  ('manager','order.edit', true, '{}'::jsonb),
  ('manager','order.cancel', true, '{}'::jsonb),
  ('manager','order.read', true, '{}'::jsonb),
  ('manager','orders.create', true, '{}'::jsonb),
  ('manager','orders.read', true, '{}'::jsonb),
  ('manager','orders.update', true, '{}'::jsonb),
  ('manager','billing.generate', true, '{}'::jsonb),
  ('manager','billing.discount', true, '{"max_discount_percent": 25}'::jsonb),
  ('manager','payment.create', true, '{}'::jsonb),
  ('manager','payments.create', true, '{}'::jsonb),
  ('manager','table.read', true, '{}'::jsonb),
  ('manager','table.start', true, '{}'::jsonb),
  ('manager','table.close', true, '{}'::jsonb),
  ('manager','table.manage', true, '{}'::jsonb),
  ('manager','tables.manage', true, '{}'::jsonb),
  ('manager','inventory.read', true, '{}'::jsonb),
  ('manager','inventory.update', true, '{}'::jsonb),
  ('manager','inventory.manage', true, '{}'::jsonb),
  ('manager','kitchen.read', true, '{}'::jsonb),
  ('manager','kitchen.update', true, '{}'::jsonb),
  ('manager','kitchen_station.read', true, '{}'::jsonb),
  ('manager','kitchen_station.manage', true, '{}'::jsonb),
  ('manager','payment.refund', true, '{"max_refund_amount": 5000}'::jsonb),
  ('manager','payments.refund', true, '{"max_refund_amount": 5000}'::jsonb),
  -- Cashier
  ('cashier','order.read', true, '{}'::jsonb),
  ('cashier','order.edit', true, '{}'::jsonb),
  ('cashier','orders.read', true, '{}'::jsonb),
  ('cashier','orders.update', true, '{}'::jsonb),
  ('cashier','billing.generate', true, '{}'::jsonb),
  ('cashier','billing.discount', true, '{"max_discount_percent": 10}'::jsonb),
  ('cashier','payment.create', true, '{}'::jsonb),
  ('cashier','payments.create', true, '{}'::jsonb),
  ('cashier','table.read', true, '{}'::jsonb),
  ('cashier','table.start', true, '{}'::jsonb),
  ('cashier','table.close', true, '{}'::jsonb),
  ('cashier','table.manage', true, '{}'::jsonb),
  ('cashier','tables.manage', true, '{}'::jsonb),
  -- Waiter
  ('waiter','order.create', true, '{}'::jsonb),
  ('waiter','order.read', true, '{}'::jsonb),
  ('waiter','orders.create', true, '{}'::jsonb),
  ('waiter','orders.read', true, '{}'::jsonb),
  ('waiter','table.read', true, '{}'::jsonb),
  ('waiter','table.start', true, '{}'::jsonb),
  ('waiter','table.close', true, '{}'::jsonb),
  ('waiter','table.manage', true, '{}'::jsonb),
  ('waiter','tables.manage', true, '{}'::jsonb),
  ('waiter','kitchen.read', true, '{}'::jsonb),
  -- Kitchen
  ('kitchen','order.read', true, '{}'::jsonb),
  ('kitchen','orders.read', true, '{}'::jsonb),
  ('kitchen','kitchen.read', true, '{}'::jsonb),
  ('kitchen','kitchen.update', true, '{}'::jsonb),
  ('kitchen','kitchen_station.read', true, '{}'::jsonb),
  -- Staff
  ('staff','order.read', true, '{}'::jsonb),
  ('staff','orders.read', true, '{}'::jsonb),
  ('staff','table.read', true, '{}'::jsonb),
  ('staff','kitchen.read', true, '{}'::jsonb)
)
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, rp.allowed, rp.meta
FROM role_perm rp
JOIN roles r ON lower(r.name) = lower(rp.role_name)
JOIN permissions p ON p.key = rp.perm_key
ON CONFLICT (role_id, permission_id)
DO UPDATE SET allowed = EXCLUDED.allowed, meta = EXCLUDED.meta, updated_at = NOW();

COMMIT;
