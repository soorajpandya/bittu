"""Run migration 037: Merchant Ledger (Phase 1 fintech reconciliation).

Applies the schema, then OPTIONALLY seeds an `opening_balance` ledger entry
per existing restaurant from the live wallet snapshot.

Usage:
    python _run_migration_037.py            # schema only
    python _run_migration_037.py --seed     # schema + seed opening balances

Re-running is safe: schema is idempotent; opening-balance seeding uses an
idempotency key per restaurant so it cannot double-post.
"""
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
from pathlib import Path

from app.core.database import close_db_pool, get_connection, init_db_pool

SQL_FILE = Path("migrations/037_merchant_ledger.sql")


async def _apply_schema() -> None:
    sql = SQL_FILE.read_text(encoding="utf-8")
    async with get_connection() as c:
        await c.execute(sql)

    async with get_connection() as c:
        n_perms = await c.fetchval(
            "SELECT COUNT(*) FROM permissions WHERE key LIKE 'merchant_ledger.%'"
        )
        n_parts = await c.fetchval(
            "SELECT COUNT(*) FROM pg_inherits "
            "WHERE inhparent = 'merchant_ledger'::regclass"
        )
        print(f"  permissions seeded: {n_perms}")
        print(f"  partitions present: {n_parts}")


async def _seed_opening_balances() -> None:
    """
    For each restaurant, post a single `opening_balance` credit entry equal
    to the merchant's current settled-net wallet position. Idempotent via
    `opening_balance:{restaurant_id}`.

    Uses a deliberately simple proxy for "current wallet balance":
        sum(net_settlement_amount where settlement_status='settled')
      + sum(payments.amount where method in cash and status='completed')
      - sum(payments.amount where method in cash and status='refunded')

    This is the "money the merchant currently has in hand or in bank from
    the platform" — the natural opening for the new ledger.
    """
    async with get_connection() as c:
        rows = await c.fetch(
            """
            WITH cash AS (
              SELECT restaurant_id,
                     COALESCE(SUM(amount) FILTER (WHERE status='completed'), 0)
                   - COALESCE(SUM(amount) FILTER (WHERE status='refunded'),  0)
                     AS net_cash
                FROM payments
               WHERE method IN ('cash','counter','cod','Cash','COD')
               GROUP BY restaurant_id
            ),
            settled AS (
              SELECT restaurant_id,
                     COALESCE(SUM(net_settlement_amount), 0) AS net_settled
                FROM bittu_settlements
               WHERE settlement_status = 'settled'
               GROUP BY restaurant_id
            ),
            all_r AS (
              SELECT restaurant_id FROM cash
              UNION
              SELECT restaurant_id FROM settled
            )
            SELECT a.restaurant_id,
                   COALESCE(c.net_cash,    0) + COALESCE(s.net_settled, 0)
                     AS opening_balance
              FROM all_r a
              LEFT JOIN cash    c USING (restaurant_id)
              LEFT JOIN settled s USING (restaurant_id)
            """
        )

    if not rows:
        print("  no restaurants with prior money movement — nothing to seed")
        return

    posted = 0
    skipped = 0
    async with get_connection() as c:
        for r in rows:
            rid = r["restaurant_id"]
            opening = Decimal(str(r["opening_balance"] or 0))
            if opening <= 0:
                skipped += 1
                continue
            result = await c.fetchval(
                """
                SELECT fn_post_merchant_ledger_entry(
                    p_merchant_id      => $1::uuid,
                    p_branch_id        => NULL,
                    p_transaction_type => 'opening_balance',
                    p_debit_amount     => 0,
                    p_credit_amount    => $2::numeric,
                    p_currency         => 'INR',
                    p_source_type      => 'migration_037_seed',
                    p_idempotency_key  => $3::text,
                    p_metadata         => $4::jsonb
                )
                """,
                rid,
                opening,
                f"opening_balance:{rid}",
                '{"source":"migration_037","note":"derived from settled+cash net"}',
            )
            if result is not None:
                posted += 1

    print(f"  opening_balance entries posted: {posted}, skipped (zero): {skipped}")


async def main(seed: bool) -> None:
    await init_db_pool()
    try:
        print(f"Applying {SQL_FILE} ...")
        await _apply_schema()
        if seed:
            print("Seeding opening balances ...")
            await _seed_opening_balances()
        print("Done.")
    finally:
        await close_db_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Also seed opening_balance entries from current wallet snapshot.",
    )
    args = parser.parse_args()
    asyncio.run(main(args.seed))
