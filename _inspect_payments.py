import asyncio
from app.core.database import init_db_pool, close_db_pool, get_connection

ORDER_IDS = [
    "31fafcc8-0022-4bee-8d24-2078efd38a83",
    "ae1fd748-0634-4ddf-96d4-23fcf63b7e6d",
]
RESTAURANT_ID = "751c6d1d-1559-45f2-a24b-7ecd16678113"

async def m():
    await init_db_pool()
    async with get_connection() as c:
        print("== payments for these orders ==")
        rows = await c.fetch(
            "SELECT id, order_id, restaurant_id, method, amount, status, settlement_id, created_at "
            "FROM payments WHERE order_id::text = ANY($1::text[]) ORDER BY created_at",
            ORDER_IDS,
        )
        for r in rows:
            print(dict(r))
        if not rows:
            print("(no rows)")

        print("\n== last 5 payments for restaurant ==")
        rows = await c.fetch(
            "SELECT id, order_id, method, amount, status, created_at FROM payments "
            "WHERE restaurant_id = $1 ORDER BY created_at DESC LIMIT 5",
            RESTAURANT_ID,
        )
        for r in rows:
            print(dict(r))
        if not rows:
            print("(no rows)")

        print("\n== payments columns ==")
        cols = await c.fetch(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name='payments' ORDER BY ordinal_position"
        )
        for col in cols:
            print(col["column_name"], col["data_type"], col["is_nullable"])
    await close_db_pool()

asyncio.run(m())
