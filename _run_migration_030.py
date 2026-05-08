"""Run migration 030 — reconciliation engine (webhook ledger + runs + discrepancies)."""
import asyncio, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


async def main():
    from app.core.database import init_db_pool, close_db_pool, get_connection

    await init_db_pool()
    sql = pathlib.Path("migrations/030_reconciliation_engine.sql").read_text()
    async with get_connection() as conn:
        for tbl in ("webhook_events", "reconciliation_runs", "reconciliation_discrepancies"):
            before = await conn.fetchval(f"SELECT to_regclass('public.{tbl}')")
            print(f"Before: {tbl} = {before}")
        await conn.execute(sql)
        for tbl in ("webhook_events", "reconciliation_runs", "reconciliation_discrepancies"):
            after = await conn.fetchval(f"SELECT to_regclass('public.{tbl}')")
            print(f"After:  {tbl} = {after}")
    print("Migration 030 applied OK")
    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
