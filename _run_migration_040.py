"""Run migration 040: payouts/disbursement engine."""
import asyncio
from pathlib import Path
from app.core.database import init_db_pool, close_db_pool, get_connection

SQL_FILE = Path("migrations/040_payouts_disbursement.sql")


async def m():
    sql = SQL_FILE.read_text(encoding="utf-8")
    await init_db_pool()
    async with get_connection() as c:
        await c.execute(sql)
        n_perms = await c.fetchval(
            "SELECT COUNT(*) FROM permissions WHERE key LIKE 'payout.%'"
        )
        n_enums = await c.fetchval(
            "SELECT COUNT(*) FROM pg_type WHERE typname IN "
            "('payout_status','payout_method','payout_beneficiary_type',"
            "'payout_batch_status','payout_event_type')"
        )
        n_tables = await c.fetchval(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name IN ('payout_beneficiaries','payout_requests',"
            "'payout_batches','payout_status_events','payout_reference_seq',"
            "'payout_batch_seq')"
        )
        print(f"perms={n_perms} enums={n_enums} tables={n_tables}")
    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(m())
