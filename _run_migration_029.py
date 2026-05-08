"""Run migration 029 — checkout idempotency table + performance indexes."""
import asyncio, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


async def main():
    from app.core.database import init_db_pool, close_db_pool, get_connection

    await init_db_pool()
    sql = pathlib.Path("migrations/029_checkout_idempotency_indexes.sql").read_text()
    async with get_connection() as conn:
        # Check current state first
        before = await conn.fetchval("SELECT to_regclass('public.checkout_idempotency')")
        print(f"Before: checkout_idempotency = {before}")
        await conn.execute(sql)
        after = await conn.fetchval("SELECT to_regclass('public.checkout_idempotency')")
        print(f"After:  checkout_idempotency = {after}")
    print("Migration 029 applied OK")
    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
