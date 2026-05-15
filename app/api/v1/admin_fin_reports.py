"""Financial Reports — Admin API (Phase 8). Prefix: /admin/fin-reports.

Cross-merchant fintech analytics. Admin can omit `merchant_id` to get
platform-wide totals, or pass one to filter to a single merchant.
Also exposes the rollup-recompute endpoint.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query, Response

from app.core.auth import require_platform_admin
from app.core.exceptions import ValidationError
from app.services.reporting_service import reporting_service

router = APIRouter(prefix="/admin/fin-reports", tags=["Reports (Admin)"])


def _default_window(from_date: Optional[date], to_date: Optional[date]) -> tuple[date, date]:
    today = date.today()
    if not to_date:
        to_date = today
    if not from_date:
        from_date = to_date - timedelta(days=29)
    if from_date > to_date:
        raise ValidationError("from_date must be <= to_date")
    return from_date, to_date


@router.get("/pnl")
async def pnl(
    merchant_id: Optional[str] = Query(None),
    from_date:   Optional[date] = Query(None),
    to_date:     Optional[date] = Query(None),
    currency:    str = Query("INR", min_length=3, max_length=3),
    _ = Depends(require_platform_admin()),
):
    f, t = _default_window(from_date, to_date)
    return await reporting_service.pnl(
        merchant_id=merchant_id, from_date=f, to_date=t, currency=currency,
    )


@router.get("/pnl/csv")
async def pnl_csv(
    merchant_id: Optional[str] = Query(None),
    from_date:   Optional[date] = Query(None),
    to_date:     Optional[date] = Query(None),
    currency:    str = Query("INR", min_length=3, max_length=3),
    _ = Depends(require_platform_admin()),
):
    f, t = _default_window(from_date, to_date)
    row = await reporting_service.pnl(
        merchant_id=merchant_id, from_date=f, to_date=t, currency=currency,
    )
    out = reporting_service.dict_to_csv(row, filename=f"pnl_{f}_{t}.csv")
    return Response(
        content=out["body"],
        media_type=out["content_type"],
        headers={"Content-Disposition": f'attachment; filename="{out["filename"]}"'},
    )


@router.get("/settlements/summary")
async def settlement_summary(
    merchant_id: Optional[str] = Query(None),
    from_date:   Optional[date] = Query(None),
    to_date:     Optional[date] = Query(None),
    _ = Depends(require_platform_admin()),
):
    f, t = _default_window(from_date, to_date)
    return await reporting_service.settlement_summary(
        merchant_id=merchant_id, from_date=f, to_date=t,
    )


@router.get("/refunds/summary")
async def refund_summary(
    merchant_id: Optional[str] = Query(None),
    from_date:   Optional[date] = Query(None),
    to_date:     Optional[date] = Query(None),
    currency:    str = Query("INR", min_length=3, max_length=3),
    _ = Depends(require_platform_admin()),
):
    f, t = _default_window(from_date, to_date)
    return await reporting_service.refund_summary(
        merchant_id=merchant_id, from_date=f, to_date=t, currency=currency,
    )


@router.get("/disputes/summary")
async def dispute_summary(
    merchant_id: Optional[str] = Query(None),
    from_date:   Optional[date] = Query(None),
    to_date:     Optional[date] = Query(None),
    currency:    str = Query("INR", min_length=3, max_length=3),
    _ = Depends(require_platform_admin()),
):
    f, t = _default_window(from_date, to_date)
    return await reporting_service.dispute_summary(
        merchant_id=merchant_id, from_date=f, to_date=t, currency=currency,
    )


@router.get("/daily")
async def daily(
    merchant_id: Optional[str] = Query(None),
    from_date:   Optional[date] = Query(None),
    to_date:     Optional[date] = Query(None),
    currency:    str = Query("INR", min_length=3, max_length=3),
    _ = Depends(require_platform_admin()),
):
    f, t = _default_window(from_date, to_date)
    return await reporting_service.daily_rollups(
        merchant_id=merchant_id, from_date=f, to_date=t, currency=currency,
    )


@router.get("/daily/csv")
async def daily_csv(
    merchant_id: Optional[str] = Query(None),
    from_date:   Optional[date] = Query(None),
    to_date:     Optional[date] = Query(None),
    currency:    str = Query("INR", min_length=3, max_length=3),
    _ = Depends(require_platform_admin()),
):
    f, t = _default_window(from_date, to_date)
    rows = await reporting_service.daily_rollups(
        merchant_id=merchant_id, from_date=f, to_date=t, currency=currency,
    )
    out = reporting_service.to_csv(rows, filename=f"daily_rollups_{f}_{t}.csv")
    return Response(
        content=out["body"],
        media_type=out["content_type"],
        headers={"Content-Disposition": f'attachment; filename="{out["filename"]}"'},
    )


@router.get("/monthly")
async def monthly(
    merchant_id: Optional[str] = Query(None),
    from_date:   Optional[date] = Query(None),
    to_date:     Optional[date] = Query(None),
    currency:    str = Query("INR", min_length=3, max_length=3),
    _ = Depends(require_platform_admin()),
):
    f, t = _default_window(from_date, to_date)
    return await reporting_service.monthly_rollups(
        merchant_id=merchant_id, from_date=f, to_date=t, currency=currency,
    )


# ────────────────────────────────────────────────────────────────────────────
# Recompute a single (merchant, date, currency) rollup. Admin-only.
# ────────────────────────────────────────────────────────────────────────────
@router.post("/rollups/compute")
async def compute_rollup(
    body: dict = Body(...),
    admin = Depends(require_platform_admin()),
):
    merchant_id = body.get("merchant_id")
    rollup_date = body.get("rollup_date")
    currency    = body.get("currency", "INR")
    if not merchant_id or not rollup_date:
        raise ValidationError("merchant_id and rollup_date are required.")
    if isinstance(rollup_date, str):
        rollup_date = date.fromisoformat(rollup_date)
    return await reporting_service.compute_daily_rollup(
        merchant_id=merchant_id,
        rollup_date=rollup_date,
        currency=currency,
        computed_by=getattr(admin, "user_id", None),
    )


# ════════════════════════════════════════════════════════════════════════════
# Platform revenue & merchant earnings (cockpit views).
# Reads `fee_computations` (immutable per-payment fee log) and `payments`
# directly so totals are always current — no dependence on rollup freshness.
# ════════════════════════════════════════════════════════════════════════════
from app.core.database import get_connection  # noqa: E402  (kept local to this section)


@router.get("/platform-revenue")
async def platform_revenue(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    currency:  str            = Query("INR", min_length=3, max_length=3),
    top_merchants: int        = Query(10, ge=1, le=100),
    _ = Depends(require_platform_admin()),
):
    """
    Platform fee revenue over a window.

    Returns:
      • totals — {fee, gst, total_deduction, gross_amount, payment_count}
      • by_merchant — top-N merchants by fee contribution
      • by_method   — split by payment_method
    """
    f, t = _default_window(from_date, to_date)
    # Inclusive lower / exclusive upper for clean date math.
    upper = t + timedelta(days=1)

    base_filter = (
        "WHERE computed_at >= $1 AND computed_at < $2 AND currency = $3"
    )
    args = [f, upper, currency.upper()]

    async with get_connection() as conn:
        totals = await conn.fetchrow(
            f"""
            SELECT COALESCE(SUM(fee_amount), 0)::numeric(18,2)      AS fee,
                   COALESCE(SUM(gst_amount), 0)::numeric(18,2)      AS gst,
                   COALESCE(SUM(total_deduction), 0)::numeric(18,2) AS total_deduction,
                   COALESCE(SUM(gross_amount), 0)::numeric(18,2)    AS gross_amount,
                   count(*)::int                                    AS payment_count
              FROM fee_computations
              {base_filter}
            """,
            *args,
        )

        by_merchant = await conn.fetch(
            f"""
            SELECT fc.merchant_id::text                              AS merchant_id,
                   r.name                                            AS restaurant_name,
                   COALESCE(SUM(fc.fee_amount), 0)::numeric(18,2)    AS fee,
                   COALESCE(SUM(fc.gst_amount), 0)::numeric(18,2)    AS gst,
                   COALESCE(SUM(fc.gross_amount), 0)::numeric(18,2)  AS gross_amount,
                   count(*)::int                                     AS payment_count
              FROM fee_computations fc
              LEFT JOIN restaurants r ON r.id = fc.merchant_id
              {base_filter}
             GROUP BY fc.merchant_id, r.name
             ORDER BY fee DESC
             LIMIT $4
            """,
            *args, top_merchants,
        )

        by_method = await conn.fetch(
            f"""
            SELECT COALESCE(payment_method, 'unknown')               AS payment_method,
                   COALESCE(SUM(fee_amount), 0)::numeric(18,2)       AS fee,
                   COALESCE(SUM(gross_amount), 0)::numeric(18,2)     AS gross_amount,
                   count(*)::int                                     AS payment_count
              FROM fee_computations
              {base_filter}
             GROUP BY payment_method
             ORDER BY fee DESC
            """,
            *args,
        )

    return {
        "from_date": f,
        "to_date":   t,
        "currency":  currency.upper(),
        "totals":    {k: float(v) if k != "payment_count" else v for k, v in dict(totals).items()},
        "by_merchant": [
            {**dict(r),
             "fee":          float(r["fee"]),
             "gst":          float(r["gst"]),
             "gross_amount": float(r["gross_amount"])}
            for r in by_merchant
        ],
        "by_method": [
            {**dict(r),
             "fee":          float(r["fee"]),
             "gross_amount": float(r["gross_amount"])}
            for r in by_method
        ],
    }


@router.get("/merchant-earnings")
async def merchant_earnings(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    currency:  str            = Query("INR", min_length=3, max_length=3),
    limit:     int            = Query(50, ge=1, le=500),
    offset:    int            = Query(0, ge=0),
    sort_by:   str            = Query("net_earnings",
        description="net_earnings|gross_sales|fees_paid|payouts_received"),
    _ = Depends(require_platform_admin()),
):
    """
    Per-merchant earnings leaderboard:
      • gross_sales       — sum of paid `payments.amount`
      • fees_paid         — sum of fee_computations.total_deduction
      • net_earnings      — gross_sales − fees_paid
      • payouts_received  — sum of bittu_settlements.net_settlement_amount
                            for status='settled'

    All windows are aligned to `created_at`. Currency is filtered on
    fee_computations + payments + bittu_settlements simultaneously.
    """
    f, t = _default_window(from_date, to_date)
    upper = t + timedelta(days=1)
    cur = currency.upper()

    sort_col = {
        "net_earnings":     "net_earnings",
        "gross_sales":      "gross_sales",
        "fees_paid":        "fees_paid",
        "payouts_received": "payouts_received",
    }.get(sort_by)
    if not sort_col:
        raise ValidationError("Invalid sort_by")

    sql = f"""
        WITH sales AS (
            SELECT restaurant_id,
                   COALESCE(SUM(amount), 0)::numeric(18,2) AS gross_sales,
                   count(*)::int                            AS payment_count
              FROM payments
             WHERE status = 'paid'
               AND currency = $3
               AND created_at >= $1 AND created_at < $2
             GROUP BY restaurant_id
        ),
        fees AS (
            SELECT merchant_id AS restaurant_id,
                   COALESCE(SUM(total_deduction), 0)::numeric(18,2) AS fees_paid,
                   COALESCE(SUM(gst_amount), 0)::numeric(18,2)      AS gst_paid
              FROM fee_computations
             WHERE currency = $3
               AND computed_at >= $1 AND computed_at < $2
             GROUP BY merchant_id
        ),
        payouts AS (
            SELECT restaurant_id,
                   COALESCE(SUM(net_settlement_amount), 0)::numeric(18,2) AS payouts_received,
                   count(*)::int                                          AS settlement_count
              FROM bittu_settlements
             WHERE settlement_status = 'settled'
               AND created_at >= $1 AND created_at < $2
             GROUP BY restaurant_id
        ),
        merged AS (
            SELECT r.id::text                                  AS merchant_id,
                   r.name                                      AS restaurant_name,
                   u.email                                     AS owner_email,
                   COALESCE(s.gross_sales, 0)::numeric(18,2)   AS gross_sales,
                   COALESCE(s.payment_count, 0)                AS payment_count,
                   COALESCE(f.fees_paid, 0)::numeric(18,2)     AS fees_paid,
                   COALESCE(f.gst_paid, 0)::numeric(18,2)      AS gst_paid,
                   (COALESCE(s.gross_sales, 0) - COALESCE(f.fees_paid, 0))::numeric(18,2)
                                                               AS net_earnings,
                   COALESCE(p.payouts_received, 0)::numeric(18,2) AS payouts_received,
                   COALESCE(p.settlement_count, 0)             AS settlement_count
              FROM restaurants r
              LEFT JOIN auth.users u ON u.id::text = r.owner_id::text
              LEFT JOIN sales   s ON s.restaurant_id = r.id
              LEFT JOIN fees    f ON f.restaurant_id = r.id
              LEFT JOIN payouts p ON p.restaurant_id = r.id
             WHERE COALESCE(s.gross_sales, 0) + COALESCE(f.fees_paid, 0)
                 + COALESCE(p.payouts_received, 0) > 0
        )
        SELECT *
          FROM merged
         ORDER BY {sort_col} DESC
         LIMIT $4 OFFSET $5
    """
    count_sql = """
        WITH sales AS (
            SELECT restaurant_id FROM payments
             WHERE status='paid' AND currency=$3
               AND created_at >= $1 AND created_at < $2
             GROUP BY restaurant_id
        ),
        fees AS (
            SELECT merchant_id AS restaurant_id FROM fee_computations
             WHERE currency=$3 AND computed_at >= $1 AND computed_at < $2
             GROUP BY merchant_id
        ),
        payouts AS (
            SELECT restaurant_id FROM bittu_settlements
             WHERE settlement_status='settled'
               AND created_at >= $1 AND created_at < $2
             GROUP BY restaurant_id
        )
        SELECT count(DISTINCT r.id)
          FROM restaurants r
          LEFT JOIN sales   s ON s.restaurant_id = r.id
          LEFT JOIN fees    f ON f.restaurant_id = r.id
          LEFT JOIN payouts p ON p.restaurant_id = r.id
         WHERE s.restaurant_id IS NOT NULL
            OR f.restaurant_id IS NOT NULL
            OR p.restaurant_id IS NOT NULL
    """

    async with get_connection() as conn:
        rows  = await conn.fetch(sql, f, upper, cur, limit, offset)
        total = await conn.fetchval(count_sql, f, upper, cur)

    items = []
    for r in rows:
        d = dict(r)
        for k in ("gross_sales", "fees_paid", "gst_paid",
                  "net_earnings", "payouts_received"):
            d[k] = float(d[k])
        items.append(d)

    return {
        "from_date": f,
        "to_date":   t,
        "currency":  cur,
        "sort_by":   sort_by,
        "items":     items,
        "limit":     limit,
        "offset":    offset,
        "total":     total,
    }
