import asyncio
from app.core.database import init_db_pool, close_db_pool, get_connection
async def m():
    await init_db_pool()
    async with get_connection() as c:
        rows = await c.fetch("SELECT id, restaurant_id, amount, status, currency, order_id FROM payments WHERE restaurant_id = '751c6d1d-1559-45f2-a24b-7ecd16678113' ORDER BY created_at DESC LIMIT 5")
        for r in rows: print(dict(r))
        if not rows:
            print("no payments")
    await close_db_pool()
asyncio.run(m())
