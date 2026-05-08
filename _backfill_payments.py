"""Backfill payments rows for orders that have payment_method in metadata but no payments row."""
import asyncio
import uuid
from datetime import datetime, timezone
from app.core.database import init_db_pool, close_db_pool, get_connection

_PM_ALIASES = {
    "cash": "cash", "counter": "cash", "cod": "cash",
    "upi": "upi", "qr_pay": "upi", "qr": "upi", "qr_code": "upi",
    "card": "card", "swipe": "card", "credit": "card", "debit": "card",
    "wallet": "wallet",
    "online": "online", "razorpay": "online", "gateway": "online", "netbanking": "online",
}

async def m():
    await init_db_pool()
    async with get_connection() as c:
        # Find orders with payment_method in metadata but no payments row.
        rows = await c.fetch(
            """
            SELECT o.id, o.user_id, o.branch_id, o.restaurant_id, o.total_amount,
                   o.metadata->>'payment_method' AS pm, o.created_at
            FROM   orders o
            LEFT   JOIN payments p ON p.order_id = o.id
            WHERE  o.metadata ? 'payment_method'
            AND    p.id IS NULL
            ORDER  BY o.created_at
            """
        )
        print(f"Found {len(rows)} orders to backfill")
        inserted = 0
        for r in rows:
            pm = _PM_ALIASES.get(str(r["pm"] or "").strip().lower(), "cash")
            status = "pending" if pm == "online" else "completed"
            paid_at = None if status == "pending" else r["created_at"]
            try:
                await c.execute(
                    """
                    INSERT INTO payments (
                        id, order_id, restaurant_id, user_id, branch_id,
                        method, status, amount, currency, paid_at, created_at, updated_at
                    ) VALUES ($1,$2,$3,$4,$5,$6::payment_method,$7::payment_status,$8,'INR',$9,$10,$10)
                    """,
                    str(uuid.uuid4()), r["id"], r["restaurant_id"], r["user_id"], r["branch_id"],
                    pm, status, float(r["total_amount"]), paid_at, r["created_at"],
                )
                if status == "completed":
                    await c.execute(
                        "UPDATE orders SET status='Confirmed', updated_at=now() WHERE id=$1 AND status='Pending'",
                        r["id"],
                    )
                inserted += 1
                print(f"  + {r['id']}  method={pm} status={status} amount={r['total_amount']}")
            except Exception as e:
                print(f"  ! {r['id']}  failed: {e}")
        print(f"Backfilled {inserted} payment rows")
    await close_db_pool()

asyncio.run(m())
