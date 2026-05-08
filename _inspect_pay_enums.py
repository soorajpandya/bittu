import asyncio
from app.core.database import init_db_pool, close_db_pool, get_connection

async def m():
    await init_db_pool()
    async with get_connection() as c:
        for name in ("payment_method", "payment_status"):
            try:
                rows = await c.fetch(
                    "SELECT enumlabel FROM pg_enum WHERE enumtypid=(SELECT oid FROM pg_type WHERE typname=$1) ORDER BY enumsortorder",
                    name,
                )
                print(name, "=", [r["enumlabel"] for r in rows])
            except Exception as e:
                print(name, "err:", e)
    await close_db_pool()

asyncio.run(m())
