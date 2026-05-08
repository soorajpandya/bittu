"""Run migration 033: disable RLS on realtime-published tables."""
import asyncio
from pathlib import Path
from app.core.database import init_db_pool, close_db_pool, get_connection

SQL_FILE = Path("migrations/033_realtime_disable_rls.sql")

async def m():
    sql = SQL_FILE.read_text()
    await init_db_pool()
    async with get_connection() as c:
        await c.execute(sql)
        rows = await c.fetch(
            "SELECT relname, relrowsecurity FROM pg_class "
            "WHERE relname IN ('orders','payments','bittu_settlements',"
            "'bittu_settlement_transactions','bittu_settlement_timeline',"
            "'pg_settlements','reconciliation_runs','reconciliation_discrepancies') "
            "ORDER BY relname"
        )
        print("RLS status:")
        for r in rows:
            print(f"  {r['relname']:40s} rls={r['relrowsecurity']}")
    await close_db_pool()

asyncio.run(m())
