"""Apply migration 046 — Fee Engine v2."""
import asyncio
from pathlib import Path
from app.core.database import init_db_pool, close_db_pool, get_connection

SQL_FILE = Path(__file__).parent / "migrations" / "046_fee_engine_v2.sql"


async def main():
    await init_db_pool()
    sql = SQL_FILE.read_text(encoding="utf-8")
    async with get_connection() as c:
        await c.execute(sql)
        perms = await c.fetchval(
            "SELECT COUNT(*) FROM permissions WHERE key LIKE 'fee_plans.%'"
        )
        tables = await c.fetchval(
            """
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema='public'
              AND table_name IN ('fee_plans','fee_plan_rules',
                                 'merchant_fee_overrides','fee_computations')
            """
        )
        fns = await c.fetchval(
            "SELECT COUNT(*) FROM pg_proc WHERE proname LIKE 'fn_%fee%' OR proname='fn_resolve_fee_plan'"
        )
        default_plan = await c.fetchval(
            "SELECT code FROM fee_plans WHERE is_default = true"
        )
        rule_count = await c.fetchval(
            "SELECT COUNT(*) FROM fee_plan_rules"
        )
        print(f"perms={perms} tables={tables} fns={fns} default_plan={default_plan} rules={rule_count}")
    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
