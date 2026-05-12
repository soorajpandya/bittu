"""Run migration 042: Phase 6 audit log."""
import asyncio
from pathlib import Path
from app.core.database import init_db_pool, close_db_pool, get_connection

SQL_FILE = Path("migrations/042_audit_log.sql")


async def m():
    sql = SQL_FILE.read_text(encoding="utf-8")
    await init_db_pool()
    async with get_connection() as c:
        await c.execute(sql)
        n_perms = await c.fetchval(
            "SELECT COUNT(*) FROM permissions WHERE key IN "
            "('audit.read','audit.read.all','audit.verify')"
        )
        n_tables = await c.fetchval(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name = 'audit_events'"
        )
        n_fns = await c.fetchval(
            "SELECT COUNT(*) FROM pg_proc WHERE proname IN "
            "('fn_append_audit_event','fn_verify_audit_chain',"
            "'fn_audit_events_append_only')"
        )
        n_idx = await c.fetchval(
            "SELECT COUNT(*) FROM pg_indexes "
            "WHERE tablename = 'audit_events'"
        )
        print(f"perms={n_perms} tables={n_tables} fns={n_fns} idx={n_idx}")
    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(m())
