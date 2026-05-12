-- 056: Enable RLS on all public tables (deny-all default) + drop 3 verified orphans.
--
-- WHY RLS:
--   Supabase exposes every public table via PostgREST using the anon /
--   authenticated JWT roles. With RLS disabled, ANY table is world-readable
--   through the public Supabase URL. The Bittu backend connects with the
--   service_role/postgres user via DATABASE_URL, which BYPASSES RLS — so
--   enabling RLS here does not affect any backend code path.
--
-- POLICY:
--   For every table we add a default "deny all" policy targeted at the
--   PostgREST roles (anon, authenticated). The backend keeps working because
--   the postgres role is a member of every role and is not subject to RLS
--   on its own tables.
--
-- ORPHAN DROPS (verified zero code references via grep_search):
--   - nodal_accounts                   (0 rows, no service touches it)
--   - escrow_balance_snapshots         (0 rows, no service touches it)
--   - merchant_liability_idempotency   (0 rows, no service touches it)

BEGIN;

-- ─── Drop verified orphans ───────────────────────────────────────────────
DROP TABLE IF EXISTS nodal_accounts CASCADE;
DROP TABLE IF EXISTS escrow_balance_snapshots CASCADE;
DROP TABLE IF EXISTS merchant_liability_idempotency CASCADE;

-- ─── Enable RLS + deny-all on every public table ─────────────────────────
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT c.oid::regclass::text AS qualified_name,
               c.relname             AS table_name
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relkind = 'r'              -- ordinary tables only
           AND c.relpersistence = 'p'       -- persistent (skip temp/unlogged where unsafe)
           AND c.relispartition = false     -- skip individual partitions (parent gets RLS, partitions inherit)
    LOOP
        -- Enable RLS (idempotent)
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', r.qualified_name);

        -- Drop any prior deny-all policy so re-runs are clean
        EXECUTE format(
            'DROP POLICY IF EXISTS deny_all_postgrest ON %s',
            r.qualified_name
        );

        -- Deny-all for anon + authenticated (PostgREST-facing roles).
        -- USING (false) on ALL commands → zero rows visible / writable.
        -- service_role and postgres are not in this list, so backend is unaffected.
        EXECUTE format($p$
            CREATE POLICY deny_all_postgrest ON %s
                AS RESTRICTIVE
                FOR ALL
                TO anon, authenticated
                USING (false)
                WITH CHECK (false)
        $p$, r.qualified_name);
    END LOOP;
END $$;

COMMIT;
