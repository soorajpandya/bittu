"""Run migration 036: WASTAGE_EXPENSE COA seed."""
import asyncio
from pathlib import Path
from app.core.database import init_db_pool, close_db_pool, get_connection

SQL_FILE = Path("migrations/036_inventory_accounting_accounts.sql")


async def m():
    sql = SQL_FILE.read_text(encoding="utf-8")
    await init_db_pool()
    async with get_connection() as c:
        await c.execute(sql)
        n = await c.fetchval(
            "SELECT COUNT(*) FROM chart_of_accounts WHERE system_code='WASTAGE_EXPENSE'"
        )
        print(f"WASTAGE_EXPENSE accounts seeded: {n}")
    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(m())
