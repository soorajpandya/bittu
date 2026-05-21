"""
Platform-finance rollups for Burptech super-admins.

All numbers are derived directly from `bittu_settlements`, `rzp_payments`,
`rzp_refunds` and `rzp_disputes`. No caching — these endpoints are
expected to be called from a small number of internal dashboards.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from app.core.database import get_service_connection


def _resolve_window(
    from_date: Optional[date], to_date: Optional[date],
) -> tuple[datetime, datetime]:
    if to_date is None:
        to_date = datetime.now(timezone.utc).date()
    if from_date is None:
        from_date = to_date - timedelta(days=30)
    if from_date > to_date:
        raise ValueError("from_date must be <= to_date")
    start = datetime.combine(from_date, time.min, tzinfo=timezone.utc)
    end = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return start, end


async def fee_revenue(
    *, from_date: Optional[date] = None, to_date: Optional[date] = None,
) -> dict[str, Any]:
    start, end = _resolve_window(from_date, to_date)
    async with get_service_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                AS settlement_count,
                COUNT(DISTINCT restaurant_id)           AS unique_merchants,
                COALESCE(SUM(gross_amount), 0)          AS gross_amount,
                COALESCE(SUM(bittu_fee_amount), 0)      AS bittu_fee,
                COALESCE(SUM(gst_amount), 0)            AS gst_amount,
                COALESCE(SUM(net_settlement_amount), 0) AS net_settled
              FROM bittu_settlements
             WHERE created_at >= $1 AND created_at < $2
               AND settlement_status = 'settled'
            """,
            start, end,
        )
    return {
        "window":       {"from": start, "to": end},
        "metrics":      dict(row) if row else {},
    }


async def gst_collected(
    *, from_date: Optional[date] = None, to_date: Optional[date] = None,
) -> dict[str, Any]:
    start, end = _resolve_window(from_date, to_date)
    async with get_service_connection() as conn:
        by_day = await conn.fetch(
            """
            SELECT (date_trunc('day', created_at AT TIME ZONE 'Asia/Kolkata'))::date AS day,
                   COALESCE(SUM(gst_amount), 0)         AS gst_amount,
                   COALESCE(SUM(bittu_fee_amount), 0)   AS bittu_fee,
                   COUNT(*)                             AS settlements
              FROM bittu_settlements
             WHERE created_at >= $1 AND created_at < $2
               AND settlement_status = 'settled'
             GROUP BY 1
             ORDER BY 1
            """,
            start, end,
        )
        total = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(gst_amount), 0)       AS total_gst,
                   COALESCE(SUM(bittu_fee_amount), 0) AS total_fee
              FROM bittu_settlements
             WHERE created_at >= $1 AND created_at < $2
               AND settlement_status = 'settled'
            """,
            start, end,
        )
    return {
        "window": {"from": start, "to": end},
        "total":  dict(total) if total else {},
        "by_day": [dict(r) for r in by_day],
    }


async def refund_liability() -> dict[str, Any]:
    """In-flight refunds the platform is on the hook for right now."""
    async with get_service_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                            AS pending_count,
                COALESCE(SUM(amount_paise), 0)      AS pending_amount_paise
              FROM rzp_refunds
             WHERE status = 'pending'
            """,
        )
        processed_24h = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                            AS processed_count,
                COALESCE(SUM(amount_paise), 0)      AS processed_amount_paise
              FROM rzp_refunds
             WHERE status = 'processed'
               AND processed_at >= now() - interval '24 hours'
            """,
        )
    return {
        "pending":           dict(row) if row else {},
        "processed_last_24h": dict(processed_24h) if processed_24h else {},
    }


async def dispute_exposure() -> dict[str, Any]:
    async with get_service_connection() as conn:
        by_status = await conn.fetch(
            """
            SELECT status::text                  AS status,
                   COUNT(*)                      AS count,
                   COALESCE(SUM(amount_paise),0) AS amount_paise
              FROM rzp_disputes
             GROUP BY status::text
             ORDER BY 1
            """,
        )
        open_total = await conn.fetchrow(
            """
            SELECT COUNT(*)                      AS count,
                   COALESCE(SUM(amount_paise),0) AS amount_paise
              FROM rzp_disputes
             WHERE status NOT IN ('won','lost','closed')
            """,
        )
    return {
        "open_total": dict(open_total) if open_total else {},
        "by_status":  [dict(r) for r in by_status],
    }


async def pnl(
    *, from_date: Optional[date] = None, to_date: Optional[date] = None,
) -> dict[str, Any]:
    """High-level P&L card. Combines fees + TPV + refunds + disputes."""
    start, end = _resolve_window(from_date, to_date)
    async with get_service_connection() as conn:
        settlements = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(gross_amount),0)         AS gmv,
                   COALESCE(SUM(bittu_fee_amount),0)     AS fee_revenue,
                   COALESCE(SUM(gst_amount),0)           AS gst_collected,
                   COALESCE(SUM(net_settlement_amount),0)AS net_settled,
                   COUNT(*)                              AS settlements
              FROM bittu_settlements
             WHERE created_at >= $1 AND created_at < $2
               AND settlement_status = 'settled'
            """,
            start, end,
        )
        refunds = await conn.fetchrow(
            """
            SELECT COUNT(*)                          AS count,
                   COALESCE(SUM(amount_paise),0)     AS amount_paise
              FROM rzp_refunds
             WHERE created_at >= $1 AND created_at < $2
               AND status = 'processed'
            """,
            start, end,
        )
        disputes = await conn.fetchrow(
            """
            SELECT COUNT(*)                          AS count,
                   COALESCE(SUM(amount_paise),0)     AS amount_paise
              FROM rzp_disputes
             WHERE created_at >= $1 AND created_at < $2
            """,
            start, end,
        )
        payments = await conn.fetchrow(
            """
            SELECT COUNT(*)                          AS count,
                   COALESCE(SUM(amount_paise),0)     AS amount_paise
              FROM rzp_payments
             WHERE status = 'captured'
               AND captured_at >= $1 AND captured_at < $2
            """,
            start, end,
        )
    return {
        "window":       {"from": start, "to": end},
        "settlements":  dict(settlements) if settlements else {},
        "refunds":      dict(refunds) if refunds else {},
        "disputes":     dict(disputes) if disputes else {},
        "captured_payments": dict(payments) if payments else {},
    }


async def tpv(
    *, from_date: Optional[date] = None, to_date: Optional[date] = None,
) -> dict[str, Any]:
    """Total Payment Volume bucketed by day (IST)."""
    start, end = _resolve_window(from_date, to_date)
    async with get_service_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT (date_trunc('day', captured_at AT TIME ZONE 'Asia/Kolkata'))::date AS day,
                   COUNT(*)                                AS payments,
                   COALESCE(SUM(amount_paise), 0)          AS amount_paise
              FROM rzp_payments
             WHERE status = 'captured'
               AND captured_at >= $1 AND captured_at < $2
             GROUP BY 1 ORDER BY 1
            """,
            start, end,
        )
    return {"window": {"from": start, "to": end},
            "by_day": [dict(r) for r in rows]}
