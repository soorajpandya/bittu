-- Migration 027: RBAC and auth query performance indexes
-- Targets the cold-cache path of require_permission() / _load_permission_map()
-- and _resolve_user_context() which hit DB on cache miss.
--
-- Missing before this migration:
--   1. roles(branch_id, lower(name)) - full table scan on every owner-path RBAC lookup
--   2. branch_users(user_id) WHERE is_active = true - partial index, tighter than existing plain index
--   3. branch_users(branch_id, user_id) WHERE is_active = true - for branch-scoped lookups

-- 1. Functional index on roles for the owner-path RBAC query:
--    SELECT id, name, branch_id FROM roles WHERE branch_id = $1 AND lower(name) = lower($2)
CREATE INDEX IF NOT EXISTS idx_roles_branch_lower_name
    ON roles (branch_id, lower(name));

-- 2. Partial index on branch_users filtered to active rows only.
--    Covers: WHERE user_id = $1 AND is_active = true
--    (used in both _resolve_user_context and _load_permission_map)
CREATE INDEX IF NOT EXISTS idx_branch_users_user_active
    ON branch_users (user_id)
    WHERE is_active = true;

-- 3. Partial composite index for branch-scoped RBAC:
--    WHERE user_id = $1 AND is_active = true AND branch_id = $2
CREATE INDEX IF NOT EXISTS idx_branch_users_branch_user_active
    ON branch_users (branch_id, user_id)
    WHERE is_active = true;
