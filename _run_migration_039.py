"""Run migration 039: Bank Reconciliation Engine (Phase 3)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.database import close_db_pool, get_connection, init_db_pool

SQL_FILE = Path("migrations/039_bank_reconciliation.sql")


async def _apply() -> None:
    sql = SQL_FILE.read_text(encoding="utf-8")
    async with get_connection() as c:
        await c.execute(sql)

    async with get_connection() as c:
        n_perms = await c.fetchval(
            "SELECT COUNT(*) FROM permissions WHERE key LIKE 'recon.%'"
        )
        tables = await c.fetch(
            "SELECT tablename FROM pg_tables WHERE tablename LIKE 'bank_recon_%' "
            "OR tablename = 'platform_admin_users' ORDER BY tablename"
        )
        print(f"  permissions seeded: {n_perms}")
        print(f"  tables present:")
        for r in tables:
            print(f"    - {r['tablename']}")


async def main() -> None:
    await init_db_pool()
    try:
        print(f"Applying {SQL_FILE} ...")
        await _apply()
        print("Done.")
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
