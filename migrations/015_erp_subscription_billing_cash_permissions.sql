-- Migration 015: Add ERP, subscription, billing & cash-transaction permission keys + role mappings
-- Extends the RBAC system to cover erp.py, subscriptions.py, billing.py, cash_transactions.py
BEGIN;

-- New permission keys.
INSERT INTO permissions (key) VALUES
  ('erp.read'),
  ('erp.write'),
  ('erp.shifts.read'),
  ('erp.shifts.manage'),
  ('erp.seed'),
  ('subscription.read'),
  ('subscription.write'),
  ('subscription.admin'),
  ('billing.read'),
  ('cash_transaction.read'),
  ('cash_transaction.create'),
  ('cash_transaction.delete')
ON CONFLICT (key) DO NOTHING;

-- Assign role permissions for the new keys.
WITH role_perm(role_name, perm_key, allowed, meta) AS (
  VALUES
  -- Owner: full access to all new modules
  ('owner','erp.read',                 true, '{}'::jsonb),
  ('owner','erp.write',                true, '{}'::jsonb),
  ('owner','erp.shifts.read',          true, '{}'::jsonb),
  ('owner','erp.shifts.manage',        true, '{}'::jsonb),
  ('owner','erp.seed',                 true, '{}'::jsonb),
  ('owner','subscription.read',        true, '{}'::jsonb),
  ('owner','subscription.write',       true, '{}'::jsonb),
  ('owner','subscription.admin',       true, '{}'::jsonb),
  ('owner','billing.read',             true, '{}'::jsonb),
  ('owner','cash_transaction.read',    true, '{}'::jsonb),
  ('owner','cash_transaction.create',  true, '{}'::jsonb),
  ('owner','cash_transaction.delete',  true, '{}'::jsonb),

  -- Manager: ERP read/write, shifts, cash transactions (no seed, no subscription, no billing)
  ('manager','erp.read',               true, '{}'::jsonb),
  ('manager','erp.write',              true, '{}'::jsonb),
  ('manager','erp.shifts.read',        true, '{}'::jsonb),
  ('manager','erp.shifts.manage',      true, '{}'::jsonb),
  ('manager','cash_transaction.read',  true, '{}'::jsonb),
  ('manager','cash_transaction.create',true, '{}'::jsonb),
  ('manager','cash_transaction.delete',true, '{}'::jsonb),

  -- Cashier: shifts + cash transactions (read/create only, no delete)
  ('cashier','erp.shifts.read',        true, '{}'::jsonb),
  ('cashier','erp.shifts.manage',      true, '{}'::jsonb),
  ('cashier','cash_transaction.read',  true, '{}'::jsonb),
  ('cashier','cash_transaction.create',true, '{}'::jsonb)
)
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, rp.allowed, rp.meta
FROM role_perm rp
JOIN roles r ON lower(r.name) = lower(rp.role_name)
JOIN permissions p ON p.key = rp.perm_key
ON CONFLICT (role_id, permission_id)
DO UPDATE
SET allowed = EXCLUDED.allowed,
    meta    = EXCLUDED.meta,
    updated_at = NOW();

COMMIT;
