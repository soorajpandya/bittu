"""Check existing invoices table schema."""
import asyncio
import asyncpg
import os

async def run():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'], statement_cache_size=0)
    cols = await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'invoices' ORDER BY ordinal_position"
    )
    print("=== invoices table columns ===")
    for c in cols:
        print(f"  {c['column_name']:30s} {c['data_type']}")
    await conn.close()

asyncio.run(run())
