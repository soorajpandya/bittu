"""Apply migration 045 — merchant KYC schema."""
import asyncio
from pathlib import Path
from app.core.database import init_db_pool, close_db_pool, get_connection

SQL_FILE = Path(__file__).parent / "migrations" / "045_merchant_kyc.sql"


async def main():
    await init_db_pool()
    sql = SQL_FILE.read_text(encoding="utf-8")
    async with get_connection() as c:
        await c.execute(sql)

        perms = await c.fetchval(
            "SELECT COUNT(*) FROM permissions WHERE key LIKE 'kyc.%'"
        )
        tables = await c.fetchval(
            """
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema='public'
              AND table_name LIKE 'merchant_kyc_%'
            """
        )
        fns = await c.fetchval(
            """
            SELECT COUNT(*) FROM pg_proc
            WHERE proname LIKE 'fn_kyc_%'
            """
        )
        print(f"perms={perms} tables={tables} fns={fns}")
    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
