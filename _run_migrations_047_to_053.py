"""Apply migrations 047 → 053 against Supabase, in order, idempotently.

Each migration file uses CREATE ... IF NOT EXISTS / DO $$ ... EXCEPTION blocks,
so re-running is safe. We run each in its own transaction (the SQL files
already wrap themselves in BEGIN/COMMIT).
"""
import asyncio
from pathlib import Path
from app.core.database import init_db_pool, close_db_pool, get_service_connection

MIGRATIONS = [
    "047_payment_webhook_events.sql",
    "048_audit_log_append_only.sql",
    "049_rls_tenant_isolation.sql",
    "050_double_entry_hardening.sql",
    "051_nodal_balancing.sql",
    "052_merchant_liability_ledger.sql",
    "053_financial_events.sql",
]

ROOT = Path(__file__).parent / "migrations"


async def verify(c):
    checks = {}
    checks["payment_webhook_events"] = await c.fetchval(
        "SELECT to_regclass('public.payment_webhook_events') IS NOT NULL"
    )
    checks["audit_block_trigger"] = await c.fetchval(
        "SELECT COUNT(*) FROM pg_trigger WHERE tgname LIKE 'trg_audit_%_no_%'"
    )
    checks["rls_policies"] = await c.fetchval(
        "SELECT COUNT(*) FROM pg_policies WHERE policyname LIKE '%tenant%'"
    )
    checks["journal_min_lines_trigger"] = await c.fetchval(
        "SELECT COUNT(*) FROM pg_trigger WHERE tgname='trg_validate_journal_min_lines'"
    )
    checks["nodal_accounts"] = await c.fetchval(
        "SELECT to_regclass('public.nodal_accounts') IS NOT NULL"
    )
    checks["escrow_balance_snapshots"] = await c.fetchval(
        "SELECT to_regclass('public.escrow_balance_snapshots') IS NOT NULL"
    )
    checks["merchant_liability_ledger"] = await c.fetchval(
        "SELECT to_regclass('public.merchant_liability_ledger') IS NOT NULL"
    )
    checks["financial_events"] = await c.fetchval(
        "SELECT to_regclass('public.financial_events') IS NOT NULL"
    )
    checks["fn_post_merchant_liability_entry"] = await c.fetchval(
        "SELECT COUNT(*) FROM pg_proc WHERE proname='fn_post_merchant_liability_entry'"
    )
    checks["fn_append_financial_event"] = await c.fetchval(
        "SELECT COUNT(*) FROM pg_proc WHERE proname='fn_append_financial_event'"
    )
    checks["fn_record_escrow_snapshot"] = await c.fetchval(
        "SELECT COUNT(*) FROM pg_proc WHERE proname='fn_record_escrow_snapshot'"
    )
    return checks


async def main():
    await init_db_pool()
    try:
        async with get_service_connection() as c:
            for fname in MIGRATIONS:
                p = ROOT / fname
                if not p.exists():
                    print(f"[SKIP] {fname} (not found)")
                    continue
                sql = p.read_text(encoding="utf-8")
                print(f"[RUN ] {fname} ({len(sql)} bytes)")
                try:
                    await c.execute(sql)
                    print(f"[ OK ] {fname}")
                except Exception as e:
                    print(f"[FAIL] {fname} :: {type(e).__name__}: {e}")
                    raise
            print("\n--- VERIFICATION ---")
            checks = await verify(c)
            for k, v in checks.items():
                print(f"  {k}: {v}")
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
