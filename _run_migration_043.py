"""Run migration 043: Phase 7 refunds & disputes."""
import asyncio
from pathlib import Path
from app.core.database import init_db_pool, close_db_pool, get_connection

SQL_FILE = Path("migrations/043_refunds_disputes.sql")


async def m():
    sql = SQL_FILE.read_text(encoding="utf-8")
    await init_db_pool()
    async with get_connection() as c:
        await c.execute(sql)
        n_perms = await c.fetchval(
            "SELECT COUNT(*) FROM permissions WHERE key LIKE 'refunds.%' OR key LIKE 'disputes.%'"
        )
        n_tables = await c.fetchval(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name IN ('refunds','disputes','dispute_events')"
        )
        n_fns = await c.fetchval(
            "SELECT COUNT(*) FROM pg_proc WHERE proname IN "
            "('fn_refundable_amount','fn_refunds_touch_updated_at',"
            "'fn_dispute_events_no_mutate')"
        )
        print(f"perms={n_perms} tables={n_tables} fns={n_fns}")
    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(m())
