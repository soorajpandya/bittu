import asyncio
from app.core.database import init_db_pool, close_db_pool, get_connection
async def m():
    await init_db_pool()
    async with get_connection() as c:
        rows = await c.fetch("SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns WHERE table_name='orders' ORDER BY ordinal_position")
        for r in rows:
            print(r['column_name'], r['data_type'], r['is_nullable'], r['column_default'])
    await close_db_pool()
asyncio.run(m())
