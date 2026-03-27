import asyncio, asyncpg, os

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], statement_cache_size=0)
    for table in ["restaurants", "google_connections", "google_posts"]:
        print(f"\n=== {table} ===")
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = $1 ORDER BY ordinal_position",
            table,
        )
        for r in rows:
            print(r["column_name"])
    await conn.close()

asyncio.run(main())
