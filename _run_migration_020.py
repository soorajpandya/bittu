import asyncio, asyncpg, os

async def run():
    url = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not url:
        print("ERROR: No DATABASE_URL or SUPABASE_DB_URL found")
        return
    conn = await asyncpg.connect(url, statement_cache_size=0)
    sql = open("migrations/020_accounting_rules_and_settlements.sql").read()
    await conn.execute(sql)
    print("Migration 020 applied successfully")
    await conn.close()

asyncio.run(run())
