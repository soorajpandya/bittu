"""Run migration 032: enable realtime publications + replica identity."""
import asyncio
from pathlib import Path
from app.core.database import init_db_pool, close_db_pool, get_connection

SQL_FILE = Path("migrations/032_realtime_publications.sql")

async def m():
    sql = SQL_FILE.read_text()
    await init_db_pool()
    async with get_connection() as c:
        await c.execute(sql)
        # Verify
        rows = await c.fetch(
            "SELECT tablename FROM pg_publication_tables WHERE pubname='supabase_realtime' ORDER BY tablename"
        )
        print("supabase_realtime tables now:")
        for r in rows:
            print(" ", r["tablename"])

        rows = await c.fetch(
            """
            SELECT c.relname AS t,
                   CASE c.relreplident
                     WHEN 'd' THEN 'default'
                     WHEN 'n' THEN 'nothing'
                     WHEN 'f' THEN 'full'
                     WHEN 'i' THEN 'index'
                   END AS ri
            FROM   pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE  n.nspname='public' AND c.relname IN (
              'orders','payments','bittu_settlements',
              'bittu_settlement_transactions','bittu_settlement_timeline',
              'pg_settlements','reconciliation_runs','reconciliation_discrepancies'
            )
            ORDER BY c.relname
            """
        )
        print("\nReplica identity:")
        for r in rows:
            print(f"  {r['t']:40s} {r['ri']}")
    await close_db_pool()

asyncio.run(m())
