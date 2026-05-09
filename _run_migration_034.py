"""Run migration 034: wallet snapshot perf indexes."""
import asyncio
from pathlib import Path
from app.core.database import init_db_pool, close_db_pool, get_connection

SQL_FILE = Path("migrations/034_wallet_snapshot_perf.sql")

async def m():
    sql = SQL_FILE.read_text()
    await init_db_pool()
    async with get_connection() as c:
        await c.execute(sql)
        rows = await c.fetch(
            "SELECT indexname FROM pg_indexes "
            "WHERE indexname IN ('idx_payments_restaurant_created',"
            "'idx_bittu_settlements_restaurant_created') "
            "ORDER BY indexname"
        )
        print("Created:")
        for r in rows:
            print(f"  {r['indexname']}")
    await close_db_pool()

asyncio.run(m())
