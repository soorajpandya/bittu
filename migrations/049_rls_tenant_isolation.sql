-- ============================================================================
-- 049_rls_tenant_isolation.sql
-- ----------------------------------------------------------------------------
-- Defense-in-depth: enforce tenant isolation at the DATABASE layer via
-- PostgreSQL Row Level Security. Application code already scopes queries via
-- `app.core.tenant.tenant_where_clause`, but a single missed filter (audit
-- finding A3.2 — `_lookup_item`) leaks across merchants. RLS makes that
-- impossible: even if a query forgets `WHERE user_id = $1`, the database
-- silently filters to rows the current tenant context owns.
--
-- WIRING:
--   * `app.core.database.get_connection()` is updated (Batch 1 code change) to
--     call `SELECT set_config('app.tenant_id', $1, true)` per acquire,
--     using the user_id from the request UserContext.
--   * Service-role connections (e.g. background workers, settlement importer)
--     run as a Postgres role that BYPASSes RLS — see
--     `app.core.database.get_service_connection()` (added in Batch 3).
--
-- THIS MIGRATION IS DEFENSIVE-ONLY:
--   * Policies use `current_setting('app.tenant_id', true)` (the trailing
--     `true` returns NULL when unset, never raises).
--   * If `app.tenant_id` is NULL, the policy is a NO-OP (allows access).
--     This means existing service code keeps working until Batch 1 wires the
--     per-request setter. After that wiring, every API request will have a
--     tenant context and RLS becomes binding.
--
-- We start with the highest-value tables. Subsequent migrations extend RLS
-- to inventory, kitchen orders, customers, etc.
-- ============================================================================

BEGIN;

-- Reusable policy helper: matches when app.tenant_id is unset OR equals user_id
CREATE OR REPLACE FUNCTION fn_rls_owner_match(row_user_id UUID) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT current_setting('app.tenant_id', true) IS NULL
        OR current_setting('app.tenant_id', true) = ''
        OR row_user_id::text = current_setting('app.tenant_id', true);
$$;

-- Apply to a table only if it exists and has a `user_id UUID` column.
CREATE OR REPLACE FUNCTION fn_apply_owner_rls(target_table TEXT) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    has_user_id BOOLEAN;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = target_table AND column_name = 'user_id'
    ) INTO has_user_id;

    IF NOT has_user_id THEN
        RAISE NOTICE 'skip RLS: table % has no user_id column', target_table;
        RETURN;
    END IF;

    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', target_table);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', target_table);

    -- Drop+recreate so this migration is idempotent.
    EXECUTE format('DROP POLICY IF EXISTS p_%s_tenant ON %I', target_table, target_table);
    EXECUTE format(
        'CREATE POLICY p_%s_tenant ON %I
            USING (fn_rls_owner_match(user_id))
            WITH CHECK (fn_rls_owner_match(user_id))',
        target_table, target_table
    );
END $$;

-- Apply to high-value tables. Each is wrapped in a DO block so the migration
-- doesn't fail if a table doesn't exist in this environment.
DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'orders',
        'order_items',
        'payments',
        'items',
        'invoices',
        'purchase_orders',
        'customers',
        'kitchen_orders'
    ] LOOP
        IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = t) THEN
            PERFORM fn_apply_owner_rls(t);
        END IF;
    END LOOP;
END $$;

COMMIT;
