"""Run migration 038: Escrow Ledger (Phase 2 fintech reconciliation).

Schema-only — no historical backfill.  Prior settled payments are not
retroactively held; escrow tracking begins only for movements posted
through the new integration helpers.

Usage:
    python _run_migration_038.py
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.database import close_db_pool, get_connection, init_db_pool

SQL_FILE = Path("migrations/038_escrow_ledger.sql")


async def _apply() -> None:
    sql = SQL_FILE.read_text(encoding="utf-8")
    async with get_connection() as c:
        await c.execute(sql)

    async with get_connection() as c:
        n_perms = await c.fetchval(
            "SELECT COUNT(*) FROM permissions WHERE key LIKE 'escrow.%'"
        )
        n_parts = await c.fetchval(
            "SELECT COUNT(*) FROM pg_inherits "
            "WHERE inhparent = 'escrow_ledger'::regclass"
        )
        print(f"  permissions seeded: {n_perms}")
        print(f"  partitions present: {n_parts}")


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
