"""Apply migration 031 (merchant wallet + fee rate)."""
import asyncio
from app.core.database import init_db_pool, close_db_pool, get_connection


async def main():
    await init_db_pool()
    try:
        with open("migrations/031_merchant_wallet_and_fee_rate.sql", "r", encoding="utf-8") as f:
            sql = f.read()
        async with get_connection() as conn:
            before = await conn.fetchval(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name='bittu_settlements' AND column_name='fee_rate'"
            )
            await conn.execute(sql)
            after = await conn.fetchval(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name='bittu_settlements' AND column_name='fee_rate'"
            )
            view_ok = await conn.fetchval(
                "SELECT to_regclass('public.merchant_wallet_snapshot')::text"
            )
            print(f"fee_rate default: {before}  →  {after}")
            print(f"merchant_wallet_snapshot view: {view_ok}")
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
