import asyncio, asyncpg, os

async def run():
    url = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    conn = await asyncpg.connect(url, statement_cache_size=0)
    exists = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='pg_settlements')"
    )
    print(f"pg_settlements exists: {exists}")
    exists2 = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='accounting_rules')"
    )
    print(f"accounting_rules exists: {exists2}")
    await conn.close()

asyncio.run(run())
