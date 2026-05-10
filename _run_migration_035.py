"""Run migration 035: inventory event system."""
import asyncio
from pathlib import Path
from app.core.database import init_db_pool, close_db_pool, get_connection

SQL_FILE = Path("migrations/035_inventory_event_system.sql")


async def m():
    sql = SQL_FILE.read_text(encoding="utf-8")
    await init_db_pool()
    async with get_connection() as c:
        await c.execute(sql)
        # Smoke check: every new table is present.
        rows = await c.fetch(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = 'public'
               AND table_name IN (
                   'inventory_snapshots','inventory_adjustments',
                   'inventory_batches','unit_conversions',
                   'inventory_alerts','inventory_counts','inventory_count_items',
                   'inventory_wastage','inventory_analytics'
               )
             ORDER BY table_name
            """
        )
        print("Inventory event-system tables present:")
        for r in rows:
            print(f"  ✓ {r['table_name']}")

        # Verify view + functions
        view_ok = await c.fetchval(
            "SELECT 1 FROM information_schema.views WHERE table_name = 'inventory_events'"
        )
        print(f"  ✓ view inventory_events: {bool(view_ok)}")

        for fn in ("fn_inventory_balance", "fn_inventory_append_event",
                   "fn_inventory_reverse_event"):
            ok = await c.fetchval(
                "SELECT 1 FROM pg_proc WHERE proname = $1", fn
            )
            print(f"  ✓ function {fn}: {bool(ok)}")
    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(m())
