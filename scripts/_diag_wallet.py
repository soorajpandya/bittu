"""Diagnose why /merchant-wallet returns zeros."""
import asyncio
from app.core.database import get_connection, init_db_pool, close_db_pool


async def main() -> None:
    await init_db_pool()
    async with get_connection() as c:
        print("=== payments by (restaurant, status, method) ===")
        rows = await c.fetch(
            "SELECT restaurant_id::text AS rid, status, method, COUNT(*) AS cnt, "
            "SUM(amount)::numeric(14,2) AS total "
            "FROM payments GROUP BY 1,2,3 ORDER BY cnt DESC LIMIT 30"
        )
        for r in rows:
            print(dict(r))

        print("\n=== bittu_settlements by (restaurant, settlement_status) ===")
        rows = await c.fetch(
            "SELECT restaurant_id::text AS rid, settlement_status, COUNT(*) AS cnt, "
            "SUM(gross_amount)::numeric(14,2) AS gross "
            "FROM bittu_settlements GROUP BY 1,2 ORDER BY cnt DESC LIMIT 30"
        )
        for r in rows:
            print(dict(r))

        print("\n=== distinct payment methods ===")
        rows = await c.fetch("SELECT DISTINCT method FROM payments")
        for r in rows:
            print(r["method"])

        print("\n=== distinct payment statuses ===")
        rows = await c.fetch("SELECT DISTINCT status FROM payments")
        for r in rows:
            print(r["status"])

    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
