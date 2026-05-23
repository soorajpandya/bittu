-- ============================================================================
-- 071_rls_strict_mode.sql
-- ----------------------------------------------------------------------------
-- Hardens `fn_rls_owner_match` (migration 049) so that the wildcard fallback
-- (NULL / empty `app.tenant_id`) only fires when the explicit opt-in GUC
-- `app.rls_bypass = 'on'` is set on the connection.
--
-- * Regular request connections set `app.tenant_id = <user_id>` and do NOT
--   set `app.rls_bypass`. Forgetting `set_config` would previously have
--   silently bypassed RLS (the wildcard branch); now it fails closed.
-- * `get_service_connection()` (background workers, cross-merchant readers)
--   must additionally set `app.rls_bypass = 'on'`. We patch the helper in
--   the same change-set.
-- ============================================================================

BEGIN;

CREATE OR REPLACE FUNCTION fn_rls_owner_match(row_user_id UUID) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT
        -- Explicit opt-in for cross-merchant readers (workers, recon jobs)
        (current_setting('app.rls_bypass', true) = 'on')
        -- OR the tenant id matches the row owner
        OR (
            current_setting('app.tenant_id', true) IS NOT NULL
            AND current_setting('app.tenant_id', true) <> ''
            AND row_user_id::text = current_setting('app.tenant_id', true)
        );
$$;

COMMIT;
