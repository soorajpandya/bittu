import asyncio
from app.core.database import init_db_pool, close_db_pool, get_connection
async def m():
    await init_db_pool()
    async with get_connection() as c:
        rows = await c.fetch("SELECT column_name FROM information_schema.columns WHERE table_name='permissions' ORDER BY ordinal_position")
        print([r['column_name'] for r in rows])
        sample = await c.fetchrow("SELECT * FROM permissions LIMIT 1")
        print(dict(sample) if sample else None)
    await close_db_pool()
asyncio.run(m())
