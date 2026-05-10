import asyncio
from app.core.database import init_db_pool, close_db_pool, get_connection


async def smoke():
    await init_db_pool()
    async with get_connection() as c:
        from app.services.inventory_service import InventoryService
        InventoryService()
        print("legacy InventoryService import OK")

        chk = await c.fetchval(
            """
            SELECT pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conrelid = 'inventory_ledger'::regclass
              AND contype='c'
              AND pg_get_constraintdef(oid) ILIKE '%transaction_type%'
            LIMIT 1
            """
        )
        print("ledger CHECK:", (chk or "")[:200])

        for t in ("consumption", "wastage", "restock_cancelled_order",
                  "transfer_in", "transfer_out", "adjustment_in",
                  "adjustment_out", "purchase", "expired"):
            in_chk = t in (chk or "")
            print(f"  type {t:30s} accepted: {in_chk}")

        n = await c.fetchval(
            "SELECT fn_inventory_balance($1, NULL, NULL)",
            "00000000-0000-0000-0000-000000000000",
        )
        print("fn_inventory_balance for unknown ing:", n)

        for t in ("inventory_snapshots", "inventory_adjustments",
                  "inventory_wastage", "inventory_alerts",
                  "inventory_counts", "inventory_batches",
                  "unit_conversions", "inventory_analytics"):
            n = await c.fetchval(f"SELECT COUNT(*) FROM {t}")
            print(f"  {t}: {n}")

        # event view sanity
        n = await c.fetchval("SELECT COUNT(*) FROM inventory_events")
        print(f"  inventory_events view rows: {n}")

    await close_db_pool()


asyncio.run(smoke())
