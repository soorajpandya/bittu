"""Apply migration 044 — reporting rollups."""
import asyncio
from pathlib import Path

from app.core.database import init_db_pool, close_db_pool, get_connection

SQL_FILE = Path(__file__).resolve().parent / "migrations" / "044_reporting_rollups.sql"


async def main():
    sql = SQL_FILE.read_text(encoding="utf-8")
    await init_db_pool()
    try:
        async with get_connection() as c:
            await c.execute(sql)
            perms = await c.fetchval(
                "SELECT COUNT(*) FROM permissions WHERE key LIKE 'reports.%'"
            )
            tables = await c.fetchval(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_name = 'merchant_daily_rollups'"
            )
            fns = await c.fetchval(
                "SELECT COUNT(*) FROM pg_proc WHERE proname = 'fn_compute_daily_rollup'"
            )
            print(f"perms={perms} tables={tables} fns={fns}")
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
