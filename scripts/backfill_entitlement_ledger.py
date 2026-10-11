"""Backfill merchant-ledger fee/GST legs (and settlement payout legs) for
captured Razorpay payments that already have a frozen Route allocation.

Idempotent: every leg uses the same deterministic keys the live path uses
(fee_deduction:{payment_id}, gst_deduction:{payment_id},
settlement_completed:{settlement_id}:{payment_id}). Payments whose
allocation is still unresolved are logged and skipped — never priced today.

Usage:
    DATABASE_URL=... .venv/bin/python scripts/backfill_entitlement_ledger.py [--dry-run] [--merchant <uuid>] [--skip-settlements]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from decimal import Decimal

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.razorpay.fee_policy import gst_split_paise  # noqa: E402

CAPTURES_SQL = """
SELECT p.id::text AS payment_id, p.restaurant_id::text AS merchant_id, p.branch_id::text AS branch_id,
       p.order_id::text AS order_id, p.razorpay_payment_id, p.amount,
       a.gross_paise, a.merchant_paise
FROM payments p
JOIN rzp_payments rp ON rp.razorpay_payment_id = p.razorpay_payment_id AND rp.captured = true
LEFT JOIN rzp_route_allocations a ON a.razorpay_payment_id = p.razorpay_payment_id
WHERE p.razorpay_payment_id IS NOT NULL
  AND ($1::uuid IS NULL OR p.restaurant_id = $1::uuid)
ORDER BY rp.captured_at NULLS LAST, rp.created_at
"""

EXISTING_KEYS_SQL = "SELECT idempotency_key FROM merchant_ledger WHERE merchant_id = $1::uuid AND payment_id = $2::uuid"

POST_SQL = """
SELECT fn_post_merchant_ledger_entry(
    p_merchant_id => $1::uuid, p_branch_id => $2::uuid,
    p_transaction_type => $3::merchant_ledger_txn_type,
    p_debit_amount => $4::numeric, p_credit_amount => 0,
    p_currency => 'INR', p_source_type => 'payment', p_source_id => NULL,
    p_settlement_id => NULL, p_payment_id => $5::uuid, p_order_id => $6::uuid,
    p_bank_reference => NULL, p_utr_number => NULL,
    p_idempotency_key => $7::text, p_metadata => $8::jsonb, p_created_by => NULL)
"""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--merchant", default=None)
    parser.add_argument("--skip-settlements", action="store_true")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_DIRECT_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL (or DATABASE_DIRECT_URL) is required", file=sys.stderr)
        return 2
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    summary = {"captures": 0, "legs_posted": 0, "receipt_missing": 0, "unresolved": [], "settlements": 0}
    try:
        await conn.execute("SELECT set_config('app.rls_bypass','on',false)")
        rows = await conn.fetch(CAPTURES_SQL, args.merchant)
        for r in rows:
            summary["captures"] += 1
            if r["merchant_paise"] is None:
                summary["unresolved"].append(r["razorpay_payment_id"])
                continue
            keys = {k["idempotency_key"] for k in await conn.fetch(EXISTING_KEYS_SQL, r["merchant_id"], r["payment_id"])}
            if f"payment_received:{r['payment_id']}" not in keys:
                # Receipt was never posted (pre-ledger history). Post gross first.
                summary["receipt_missing"] += 1
                if not args.dry_run:
                    await conn.fetchval(
                        POST_SQL.replace("p_credit_amount => 0", "p_credit_amount => $4::numeric")
                                .replace("p_debit_amount => $4::numeric", "p_debit_amount => 0"),
                        r["merchant_id"], r["branch_id"], "payment_received", Decimal(str(r["amount"])),
                        r["payment_id"], r["order_id"], f"payment_received:{r['payment_id']}",
                        json.dumps({"razorpay_payment_id": r["razorpay_payment_id"], "source": "backfill"}),
                    )
            fee, gst = gst_split_paise(int(r["gross_paise"]) - int(r["merchant_paise"]))
            for txn_type, paise in (("fee_deduction", fee), ("gst_deduction", gst)):
                key = f"{txn_type}:{r['payment_id']}"
                if paise <= 0 or key in keys:
                    continue
                if not args.dry_run:
                    await conn.fetchval(
                        POST_SQL, r["merchant_id"], r["branch_id"], txn_type, Decimal(paise) / 100,
                        r["payment_id"], r["order_id"], key,
                        json.dumps({"razorpay_payment_id": r["razorpay_payment_id"], "source": "backfill",
                                    "gross_paise": int(r["gross_paise"]), "merchant_paise": int(r["merchant_paise"]),
                                    "fee_basis": "full_consideration", "leg": txn_type}),
                    )
                summary["legs_posted"] += 1
    finally:
        await conn.close()

    if not args.skip_settlements and not args.dry_run:
        # Payout legs go through the live service so the logic stays single-sourced.
        from app.core.database import close_db_pool, init_db_pool
        from app.services.razorpay import ledgers
        await init_db_pool()
        try:
            from app.core.database import get_service_connection
            async with get_service_connection() as c:
                ids = await c.fetch(
                    "SELECT settlement_id, merchant_id::text AS merchant_id FROM rzp_settlements "
                    "WHERE status::text='processed' AND ($1::uuid IS NULL OR merchant_id=$1::uuid)",
                    args.merchant,
                )
            for s in ids:
                result = await ledgers.apply_settlement_processed(
                    settlement_id=s["settlement_id"], merchant_id=s["merchant_id"],
                )
                summary["settlements"] += int(result.get("posted") or 0)
        finally:
            await close_db_pool()
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
