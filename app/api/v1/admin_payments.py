"""
Pay-ins (admin) — cross-merchant listing of gateway payments.

Prefix:   /admin/payments
Audience: platform admins.

Reads from `payments` directly. No mutation endpoints — payments mutate
via the gateway/webhook pipeline; admins only need read + filter here.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import require_platform_admin
from app.core.database import get_connection

router = APIRouter(prefix="/admin/payments", tags=["Payments (Admin)"])


@router.get("")
async def list_payments(
    merchant_id: Optional[str]      = Query(None, description="Restaurant UUID"),
    branch_id:   Optional[str]      = Query(None),
    status:      Optional[str]      = Query(None, description="pending|paid|failed|refunded|..."),
    method:      Optional[str]      = Query(None, description="upi|card|netbanking|cash|..."),
    gateway_payment_id: Optional[str] = Query(None, description="Razorpay/Cashfree id"),
    order_id:    Optional[str]      = Query(None),
    min_amount:  Optional[float]    = Query(None, ge=0),
    max_amount:  Optional[float]    = Query(None, ge=0),
    from_ts:     Optional[datetime] = Query(None),
    to_ts:       Optional[datetime] = Query(None),
    limit:       int                = Query(100, ge=1, le=500),
    offset:      int                = Query(0, ge=0),
    _ = Depends(require_platform_admin()),
):
    """
    Cross-merchant listing of pay-ins. Returns rows joined with the
    restaurant name + owner email so the cockpit can render a single
    table without N+1 lookups.
    """
    where: list[str] = []
    args:  list = []

    def add(clause: str, value):
        args.append(value)
        where.append(clause.replace("?", f"${len(args)}"))

    if merchant_id:        add("p.restaurant_id = ?::uuid", merchant_id)
    if branch_id:          add("p.branch_id = ?::uuid", branch_id)
    if status:             add("p.status = ?", status)
    if method:             add("p.method = ?", method)
    if order_id:           add("p.order_id = ?::uuid", order_id)
    if gateway_payment_id: add("p.razorpay_payment_id = ?", gateway_payment_id)
    if min_amount is not None: add("p.amount >= ?", min_amount)
    if max_amount is not None: add("p.amount <= ?", max_amount)
    if from_ts:            add("p.created_at >= ?", from_ts)
    if to_ts:              add("p.created_at <  ?", to_ts)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT p.id::text                AS payment_id,
               p.order_id::text          AS order_id,
               p.restaurant_id::text     AS restaurant_id,
               r.name                    AS restaurant_name,
               u.email                   AS owner_email,
               p.branch_id::text         AS branch_id,
               p.user_id                 AS user_id,
               p.method,
               p.status,
               p.amount,
               p.currency,
               p.razorpay_order_id       AS gateway_order_id,
               p.razorpay_payment_id     AS gateway_payment_id,
               p.paid_at,
               p.created_at,
               p.updated_at
          FROM payments p
          LEFT JOIN restaurants r ON r.id = p.restaurant_id
          LEFT JOIN auth.users  u ON u.id::text = r.owner_id::text
          {where_sql}
         ORDER BY p.created_at DESC
         LIMIT ${len(args)+1} OFFSET ${len(args)+2}
    """
    count_sql = f"SELECT count(*) FROM payments p {where_sql}"

    async with get_connection() as conn:
        rows  = await conn.fetch(sql, *args, limit, offset)
        total = await conn.fetchval(count_sql, *args)
    return {
        "items":  [dict(r) for r in rows],
        "limit":  limit,
        "offset": offset,
        "total":  total,
    }


@router.get("/summary")
async def payments_summary(
    merchant_id: Optional[str]      = Query(None),
    from_ts:     Optional[datetime] = Query(None),
    to_ts:       Optional[datetime] = Query(None),
    _ = Depends(require_platform_admin()),
):
    """
    Aggregate pay-in totals over a window. Returns counts + sums by
    status; useful for the LiveOps + Health pages.
    """
    where: list[str] = []
    args:  list = []
    def add(clause: str, value):
        args.append(value)
        where.append(clause.replace("?", f"${len(args)}"))
    if merchant_id: add("restaurant_id = ?::uuid", merchant_id)
    if from_ts:     add("created_at >= ?", from_ts)
    if to_ts:       add("created_at <  ?", to_ts)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT status,
               count(*)::int                              AS count,
               COALESCE(SUM(amount), 0)::numeric(18,2)    AS total_amount
          FROM payments
          {where_sql}
         GROUP BY status
         ORDER BY status
    """
    async with get_connection() as conn:
        rows = await conn.fetch(sql, *args)

    by_status = {r["status"]: {"count": r["count"], "total_amount": float(r["total_amount"])} for r in rows}
    grand_total_count  = sum(v["count"] for v in by_status.values())
    grand_total_amount = sum(v["total_amount"] for v in by_status.values())
    paid = by_status.get("paid", {"count": 0, "total_amount": 0.0})
    return {
        "by_status":           by_status,
        "total_count":         grand_total_count,
        "total_amount":        grand_total_amount,
        "successful_count":    paid["count"],
        "successful_amount":   paid["total_amount"],
    }


@router.get("/{payment_id}")
async def get_payment(
    payment_id: str,
    _ = Depends(require_platform_admin()),
):
    sql = """
        SELECT p.id::text                AS payment_id,
               p.order_id::text          AS order_id,
               p.restaurant_id::text     AS restaurant_id,
               r.name                    AS restaurant_name,
               u.email                   AS owner_email,
               p.branch_id::text         AS branch_id,
               p.user_id                 AS user_id,
               p.method, p.status, p.amount, p.currency,
               p.razorpay_order_id       AS gateway_order_id,
               p.razorpay_payment_id     AS gateway_payment_id,
               p.paid_at, p.created_at, p.updated_at
          FROM payments p
          LEFT JOIN restaurants r ON r.id = p.restaurant_id
          LEFT JOIN auth.users  u ON u.id::text = r.owner_id::text
         WHERE p.id = $1::uuid
    """
    async with get_connection() as conn:
        row = await conn.fetchrow(sql, payment_id)
        if not row:
            raise HTTPException(404, "Payment not found")
        # Latest fee computation for this payment, if any.
        fee = await conn.fetchrow(
            """
            SELECT fee_amount, gst_amount, total_deduction, net_amount,
                   plan_id, rule_id, breakdown, computed_at
              FROM fee_computations
             WHERE payment_id = $1
             ORDER BY computed_at DESC
             LIMIT 1
            """,
            payment_id,
        )
    return {**dict(row), "fee_computation": dict(fee) if fee else None}
