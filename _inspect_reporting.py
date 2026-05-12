import asyncio
from app.core.database import init_db_pool, close_db_pool, get_connection

TABLES = ["bittu_settlements", "pg_settlements", "merchant_ledger", "payments", "orders", "refunds", "disputes"]

async def m():
    await init_db_pool()
    async with get_connection() as c:
        for t in TABLES:
            print(f"\n=== {t} ===")
            rows = await c.fetch(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name=$1 ORDER BY ordinal_position",
                t,
            )
            for r in rows:
                print(f"  {r['column_name']:30s} {r['data_type']}")
        # enum values for ledger txn type
        et = await c.fetch("SELECT unnest(enum_range(NULL::merchant_ledger_txn_type))::text AS v")
        print("\n=== merchant_ledger_txn_type ===")
        for r in et:
            print(" ", r["v"])
    await close_db_pool()

asyncio.run(m())
