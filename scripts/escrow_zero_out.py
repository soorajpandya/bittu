"""One-off maintenance: release every legacy escrow hold so the escrow ledger
reaches zero and stays zero (D3 — escrow retired under Razorpay Route).

Rules (idempotent, safe to re-run):
  * hold whose payment has a processed Route transfer -> escrow_release
  * hold on a cash/direct POS payment (no razorpay ids) -> escrow_release
  * processed refunds against a held payment            -> escrow_refund
    (one per refund, key escrow_refund:{refund_id}; capped at the hold amount)
  * anything else is reported as `left_open` for operations review.

Usage:
    DATABASE_URL=... .venv/bin/python scripts/escrow_zero_out.py [--dry-run] [--merchant <uuid>]
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


OPEN_HOLDS_SQL = """
SELECT h.id AS hold_id, h.merchant_id::text AS merchant_id, h.branch_id, h.currency,
       h.credit_amount, h.payment_id::text AS payment_id, h.order_id::text AS order_id,
       p.method::text AS method, p.razorpay_payment_id, p.razorpay_order_id,
       EXISTS (
         SELECT 1 FROM rzp_route_transfers t
         WHERE t.razorpay_payment_id = p.razorpay_payment_id AND t.status::text = 'processed'
       ) AS transfer_processed,
       COALESCE((
         SELECT SUM(e.debit_amount) FROM escrow_ledger e
         WHERE e.merchant_id = h.merchant_id AND e.payment_id = h.payment_id
           AND e.transaction_type IN ('escrow_refund','escrow_chargeback','escrow_expired')
       ), 0) AS already_debited
FROM escrow_ledger h
LEFT JOIN escrow_release_links rl ON rl.merchant_id = h.merchant_id AND rl.hold_entry_id = h.id
LEFT JOIN payments p ON p.id = h.payment_id
WHERE h.transaction_type = 'escrow_hold' AND rl.hold_entry_id IS NULL
  AND ($1::uuid IS NULL OR h.merchant_id = $1::uuid)
ORDER BY h.created_at
"""

REFUNDS_SQL = """
SELECT r.id, r.refund_uuid::text AS refund_uuid, r.amount
FROM refunds r
WHERE r.payment_id = $1::uuid AND r.merchant_id = $2::uuid AND r.status = 'succeeded'
ORDER BY r.processed_at NULLS LAST, r.id
"""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--merchant", default=None)
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_DIRECT_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL (or DATABASE_DIRECT_URL) is required", file=sys.stderr)
        return 2
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        await conn.execute("SELECT set_config('app.rls_bypass','on',false)")
        holds = await conn.fetch(OPEN_HOLDS_SQL, args.merchant)
        summary = {"considered": len(holds), "released": 0, "refund_debits": 0, "left_open": []}
        for h in holds:
            amount = Decimal(str(h["credit_amount"]))
            remaining = amount - Decimal(str(h["already_debited"] or 0))
            # 1. Refund debits first (partial refunds reduce what is releasable).
            if h["payment_id"]:
                for r in await conn.fetch(REFUNDS_SQL, h["payment_id"], h["merchant_id"]):
                    debit = min(Decimal(str(r["amount"])), max(remaining, Decimal(0)))
                    if debit <= 0:
                        continue
                    if not args.dry_run:
                        await conn.fetchval(
                            """
                            SELECT fn_post_escrow_ledger_entry(
                                p_merchant_id => $1::uuid, p_branch_id => $2::uuid,
                                p_transaction_type => 'escrow_refund'::escrow_txn_type,
                                p_debit_amount => $3::numeric, p_credit_amount => 0,
                                p_currency => $4::char(3), p_source_type => 'refund',
                                p_source_id => $5::uuid, p_payment_id => $6::uuid,
                                p_settlement_id => NULL, p_order_id => $7::uuid,
                                p_bank_reference => NULL, p_hold_until => NULL,
                                p_released_entry_id => NULL,
                                p_idempotency_key => $8::text,
                                p_metadata => $9::jsonb, p_created_by => NULL)
                            """,
                            h["merchant_id"], h["branch_id"], debit, h["currency"],
                            r["refund_uuid"], h["payment_id"], h["order_id"],
                            f"escrow_refund:{r['refund_uuid']}",
                            json.dumps({"reason": "escrow_retired_backfill", "hold_id": str(h["hold_id"])}),
                        )
                    remaining -= debit
                    summary["refund_debits"] += 1
            kind_ok = bool(h["transfer_processed"]) or (
                not h["razorpay_payment_id"] and not h["razorpay_order_id"]
            )
            if not kind_ok:
                summary["left_open"].append({
                    "hold_id": str(h["hold_id"]), "payment_id": h["payment_id"],
                    "razorpay_payment_id": h["razorpay_payment_id"], "remaining": str(remaining),
                    "reason": "no processed Route transfer yet",
                })
                continue
            if remaining <= 0:
                # Fully consumed by refund debits; nothing left to release.
                summary["consumed_by_refunds"] = summary.get("consumed_by_refunds", 0) + 1
                continue
            if not args.dry_run:
                try:
                    await conn.fetchval(
                        """
                        SELECT fn_post_escrow_ledger_entry(
                            p_merchant_id => $1::uuid, p_branch_id => $2::uuid,
                            p_transaction_type => 'escrow_release'::escrow_txn_type,
                            p_debit_amount => $3::numeric, p_credit_amount => 0,
                            p_currency => $4::char(3), p_source_type => 'release',
                            p_source_id => NULL, p_payment_id => $5::uuid,
                            p_settlement_id => NULL, p_order_id => $6::uuid,
                            p_bank_reference => NULL, p_hold_until => NULL,
                            p_released_entry_id => $7::uuid,
                            p_idempotency_key => $8::text,
                            p_metadata => $9::jsonb, p_created_by => NULL)
                        """,
                        h["merchant_id"], h["branch_id"], remaining,
                        h["currency"], h["payment_id"], h["order_id"], str(h["hold_id"]),
                        f"escrow_release:{h['hold_id']}",
                        json.dumps({"reason": "escrow_retired_route_pays_merchant_directly"}),
                    )
                except asyncpg.UniqueViolationError:
                    pass  # already released by an earlier run
            summary["released"] += 1
        print(json.dumps(summary, indent=2, default=str))
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
