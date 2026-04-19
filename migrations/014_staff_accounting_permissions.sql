-- Migration 014: Add staff & accounting permission keys + role mappings
-- Extends the RBAC system (migration 013) to cover staff.py and accounting.py endpoints.
BEGIN;

-- New permission keys for staff & accounting modules.
INSERT INTO permissions (key) VALUES
  ('staff.branches.read'),
  ('staff.branches.create'),
  ('staff.branches.update'),
  ('staff.read'),
  ('staff.create'),
  ('staff.update'),
  ('staff.delete'),
  ('staff.branch_users.read'),
  ('staff.branch_users.create'),
  ('staff.branch_users.update'),
  ('staff.branch_users.delete'),
  ('accounting.read'),
  ('accounting.write')
ON CONFLICT (key) DO NOTHING;

-- Assign role permissions for the new keys.
WITH role_perm(role_name, perm_key, allowed, meta) AS (
  VALUES
  -- Owner: full staff & accounting access
  ('owner','staff.branches.read',       true, '{}'::jsonb),
  ('owner','staff.branches.create',     true, '{}'::jsonb),
  ('owner','staff.branches.update',     true, '{}'::jsonb),
  ('owner','staff.read',                true, '{}'::jsonb),
  ('owner','staff.create',              true, '{}'::jsonb),
  ('owner','staff.update',              true, '{}'::jsonb),
  ('owner','staff.delete',              true, '{}'::jsonb),
  ('owner','staff.branch_users.read',   true, '{}'::jsonb),
  ('owner','staff.branch_users.create', true, '{}'::jsonb),
  ('owner','staff.branch_users.update', true, '{}'::jsonb),
  ('owner','staff.branch_users.delete', true, '{}'::jsonb),
  ('owner','accounting.read',           true, '{}'::jsonb),
  ('owner','accounting.write',          true, '{}'::jsonb),

  -- Manager: read staff + full accounting
  ('manager','staff.read',              true, '{}'::jsonb),
  ('manager','staff.branch_users.read', true, '{}'::jsonb),
  ('manager','accounting.read',         true, '{}'::jsonb),
  ('manager','accounting.write',        true, '{}'::jsonb)
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
