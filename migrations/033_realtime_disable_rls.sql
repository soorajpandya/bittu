-- =====================================================================
-- 033_realtime_disable_rls.sql
-- ---------------------------------------------------------------------
-- Why: tables had RLS enabled with NO policies, so Postgres denies every
-- SELECT under the anon/authenticated JWT used by Supabase Realtime,
-- and clients receive zero change events.
--
-- This is safe because:
--   - The Flutter app only talks to the FastAPI backend (not Supabase REST).
--   - The backend enforces tenant isolation in every query (restaurant_id
--     scoping via UserContext / require_permission).
--   - Realtime delivers change events but the client still calls REST to
--     fetch data, so any unauthorised access would be caught at the API.
--
-- If you ever expose Supabase REST/PostgREST directly to clients, replace
-- this with proper per-table RLS policies that join through `users` to
-- match restaurant_id against the JWT.
-- =====================================================================

BEGIN;

ALTER TABLE public.orders                        DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.payments                      DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.bittu_settlements             DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.bittu_settlement_transactions DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.bittu_settlement_timeline     DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.pg_settlements                DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.reconciliation_runs           DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.reconciliation_discrepancies  DISABLE ROW LEVEL SECURITY;

COMMIT;

-- Verify
-- SELECT relname, relrowsecurity
-- FROM   pg_class
-- WHERE  relname IN (
--   'orders','payments','bittu_settlements','bittu_settlement_transactions',
--   'bittu_settlement_timeline','pg_settlements',
--   'reconciliation_runs','reconciliation_discrepancies'
-- );
