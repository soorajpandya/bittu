-- =====================================================================
-- 032_realtime_publications.sql
-- Enable Supabase Realtime on the tables that drive the merchant wallet,
-- order list and reconciliation dashboards.
--
-- Idempotent: safe to re-run. Each ALTER PUBLICATION ADD TABLE is wrapped
-- in a DO block so re-runs (or partial failures) don't abort the script.
-- =====================================================================

BEGIN;

-- ── 1) Add tables to the supabase_realtime publication ───────────────
DO $$
DECLARE
    t text;
    targets text[] := ARRAY[
        'orders',
        'payments',
        'bittu_settlements',
        'bittu_settlement_transactions',
        'bittu_settlement_timeline',
        'pg_settlements',
        'reconciliation_runs',
        'reconciliation_discrepancies'
    ];
BEGIN
    FOREACH t IN ARRAY targets LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_publication_tables
            WHERE  pubname  = 'supabase_realtime'
            AND    schemaname = 'public'
            AND    tablename  = t
        ) THEN
            EXECUTE format('ALTER PUBLICATION supabase_realtime ADD TABLE public.%I', t);
            RAISE NOTICE 'Added % to supabase_realtime', t;
        ELSE
            RAISE NOTICE 'Skipped % (already in publication)', t;
        END IF;
    END LOOP;
END $$;

-- ── 2) REPLICA IDENTITY FULL so UPDATE/DELETE events carry full rows ─
ALTER TABLE public.orders                        REPLICA IDENTITY FULL;
ALTER TABLE public.payments                      REPLICA IDENTITY FULL;
ALTER TABLE public.bittu_settlements             REPLICA IDENTITY FULL;
ALTER TABLE public.bittu_settlement_transactions REPLICA IDENTITY FULL;
ALTER TABLE public.bittu_settlement_timeline     REPLICA IDENTITY FULL;
ALTER TABLE public.pg_settlements                REPLICA IDENTITY FULL;
ALTER TABLE public.reconciliation_runs           REPLICA IDENTITY FULL;
ALTER TABLE public.reconciliation_discrepancies  REPLICA IDENTITY FULL;

COMMIT;

-- ── 3) Verify (informational, run separately if you like) ────────────
-- SELECT schemaname, tablename
-- FROM   pg_publication_tables
-- WHERE  pubname = 'supabase_realtime'
-- ORDER  BY tablename;
--
-- SELECT c.relname AS table_name,
--        CASE c.relreplident
--          WHEN 'd' THEN 'default (primary key)'
--          WHEN 'n' THEN 'nothing'
--          WHEN 'f' THEN 'full'
--          WHEN 'i' THEN 'index'
--        END AS replica_identity
-- FROM   pg_class c
-- JOIN   pg_namespace n ON n.oid = c.relnamespace
-- WHERE  n.nspname = 'public'
-- AND    c.relname IN (
--          'orders','payments','bittu_settlements',
--          'bittu_settlement_transactions','bittu_settlement_timeline',
--          'pg_settlements','reconciliation_runs','reconciliation_discrepancies'
--        )
-- ORDER  BY c.relname;
