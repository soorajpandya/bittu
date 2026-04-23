"""Run migration 025 — Financial Product Layer."""
import asyncio, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

async def main():
    from app.core.database import init_db_pool, close_db_pool, get_connection
    await init_db_pool()
    sql = pathlib.Path("migrations/025_financial_product_layer.sql").read_text()
    async with get_connection() as conn:
        await conn.execute(sql)
    print("Migration 025 applied OK")
    await close_db_pool()

if __name__ == "__main__":
    asyncio.run(main())
