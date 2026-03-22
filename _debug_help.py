import asyncio, asyncpg, os
from dotenv import load_dotenv
load_dotenv()

async def test():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    cols = await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = $1 ORDER BY ordinal_position",
        "help_articles",
    )
    for c in cols:
        print(c["column_name"], c["data_type"])
    if not cols:
        print("Table help_articles does not exist or has no columns")
    await conn.close()

asyncio.run(test())
