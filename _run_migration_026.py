"""Run migration 026 — Performance indexes for dine-in hot paths."""
import asyncio, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


async def main():
    from app.core.database import init_db_pool, close_db_pool, get_connection

    await init_db_pool()
    sql = pathlib.Path("migrations/026_performance_indexes.sql").read_text()
    async with get_connection() as conn:
        await conn.execute(sql)
    print("Migration 026 applied OK")
    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
