"""
Per-merchant reports & invoices.

Single router that surfaces everything a merchant needs to see / download
about their own money — strictly scoped to the caller's restaurant_id.
Nothing here trusts a query-string `merchant_id`; we always derive it from
the authenticated `UserContext`.

Surface:
  GET  /merchant-reports/summary            — KPIs (P&L + wallet snapshot)
  GET  /merchant-reports/wallet             — pure wallet snapshot
  GET  /merchant-reports/transactions       — paginated payments list
  GET  /merchant-reports/transactions.csv   — same, CSV download
  GET  /merchant-reports/settlements        — Razorpay settlements (this merchant)
  GET  /merchant-reports/invoice/{order_id}.pdf       — customer tax invoice
  GET  /merchant-reports/saas-invoice/{year}/{month}.pdf — Bittu→merchant SaaS invoice
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse

from app.core.auth import UserContext, require_permission
from app.core.database import get_connection
from app.core.exceptions import NotFoundError, ValidationError
from app.services.invoice_pdf_service import (
    render_customer_invoice,
    render_saas_invoice,
)
from app.services.merchant_wallet_service import MerchantWalletService
from app.services.reporting_service import reporting_service

router = APIRouter(prefix="/merchant-reports", tags=["Merchant Reports"])

_wallet_svc = MerchantWalletService()


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────
def _mid(user: UserContext) -> str:
    if not user.restaurant_id:
        raise ValidationError("Active restaurant context is required.")
    return str(user.restaurant_id)


def _window(
    from_date: Optional[date], to_date: Optional[date], *, default_days: int = 29,
) -> tuple[date, date]:
    today = date.today()
    t = to_date or today
    f = from_date or (t - timedelta(days=default_days))
    if f > t:
        raise ValidationError("from_date must be <= to_date")
    return f, t


# ────────────────────────────────────────────────────────────────────
# Summary + wallet
# ────────────────────────────────────────────────────────────────────
@router.get("/summary")
async def summary(
    window: str = Query(
        "month", pattern="^(today|week|month|lifetime|custom)$",
        description="Quick window selector. When 'custom' is used, supply from_date/to_date.",
    ),
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    currency:  str = Query("INR", min_length=3, max_length=3),
    user: UserContext = Depends(require_permission("reports.read")),
):
    """
    KPI summary for the merchant: P&L window + wallet snapshot.

    `wallet` is the merchant's *cash position right now* (lifetime), while
    `pnl` is scoped to the chosen window — same approach as the dashboard
    expects.
    """
    today = date.today()
    if window == "today":
        f, t = today, today
    elif window == "week":
        f, t = today - timedelta(days=6), today
    elif window == "month":
        f, t = today.replace(day=1), today
    elif window == "lifetime":
        f, t = date(2020, 1, 1), today
    else:  # custom
        f, t = _window(from_date, to_date)

    pnl = await reporting_service.pnl(
        merchant_id=_mid(user), from_date=f, to_date=t, currency=currency,
    )
    wallet = await _wallet_svc.wallet(user, from_date=f, to_date=t)
    return {"window": window, "from_date": f, "to_date": t, "pnl": pnl, "wallet": wallet}


@router.get("/wallet")
async def wallet(
    as_of_date: Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("reports.read")),
):
    """Pure wallet snapshot — see MerchantWalletService.wallet for the schema."""
    return await _wallet_svc.wallet(user, as_of_date=as_of_date)


# ────────────────────────────────────────────────────────────────────
# Transactions list (per-payment ledger)
# ────────────────────────────────────────────────────────────────────
_TXN_COLUMNS = [
    "payment_id", "order_id", "created_at", "method", "status",
    "amount", "currency", "razorpay_payment_id", "razorpay_order_id",
    "customer_name", "customer_phone",
]


async def _fetch_transactions(
    *,
    merchant_id: str,
    f: date, t: date,
    method: Optional[str],
    status: Optional[str],
    limit: int, offset: int,
) -> list[dict]:
    f_ts = datetime.combine(f, datetime.min.time()).replace(tzinfo=timezone.utc)
    t_ts = datetime.combine(t, datetime.max.time()).replace(tzinfo=timezone.utc)
    sql = """
        SELECT p.id::text                    AS payment_id,
               p.order_id::text              AS order_id,
               p.created_at,
               p.method,
               p.status,
               p.amount,
               p.currency,
               p.razorpay_payment_id,
               p.razorpay_order_id,
               c.name                        AS customer_name,
               c.phone_number                AS customer_phone
        FROM payments p
        LEFT JOIN orders    o ON o.id = p.order_id
        LEFT JOIN customers c ON c.id = o.customer_id
        WHERE p.restaurant_id = $1::uuid
          AND p.created_at   >= $2::timestamptz
          AND p.created_at   <= $3::timestamptz
          AND ($4::text IS NULL OR p.method = $4::text)
          AND ($5::text IS NULL OR p.status = $5::text)
        ORDER BY p.created_at DESC
        LIMIT $6 OFFSET $7
    """
    async with get_connection() as conn:
        rows = await conn.fetch(
            sql, str(merchant_id), f_ts, t_ts, method, status, int(limit), int(offset),
        )
    return [dict(r) for r in rows]


@router.get("/transactions")
async def transactions(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    method:    Optional[str] = Query(None, description="cash|upi|card|wallet|online (exact match)"),
    status:    Optional[str] = Query(None, description="completed|pending|initiated|failed|refunded"),
    limit:     int  = Query(50, ge=1, le=500),
    offset:    int  = Query(0,  ge=0),
    user: UserContext = Depends(require_permission("reports.read")),
):
    """Paginated per-payment ledger scoped to the caller's merchant."""
    f, t = _window(from_date, to_date, default_days=29)
    rows = await _fetch_transactions(
        merchant_id=_mid(user), f=f, t=t,
        method=method, status=status,
        limit=limit, offset=offset,
    )
    return {
        "from_date": f, "to_date": t,
        "limit": limit, "offset": offset, "count": len(rows),
        "items": rows,
    }


@router.get("/transactions.csv")
async def transactions_csv(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    method:    Optional[str] = Query(None),
    status:    Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("reports.export")),
):
    """
    Full CSV dump of the per-payment ledger for the window.

    Capped at 50 000 rows per export to keep the response bounded; merchants
    needing more should narrow the date window.
    """
    f, t = _window(from_date, to_date, default_days=29)
    rows = await _fetch_transactions(
        merchant_id=_mid(user), f=f, t=t,
        method=method, status=status,
        limit=50_000, offset=0,
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_TXN_COLUMNS)
    for r in rows:
        writer.writerow([
            r.get("payment_id"),
            r.get("order_id"),
            (r.get("created_at").isoformat() if r.get("created_at") else ""),
            r.get("method"),
            r.get("status"),
            r.get("amount"),
            r.get("currency"),
            r.get("razorpay_payment_id"),
            r.get("razorpay_order_id"),
            r.get("customer_name"),
            r.get("customer_phone"),
        ])
    buf.seek(0)
    filename = f"transactions_{f}_{t}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ────────────────────────────────────────────────────────────────────
# Settlements (Razorpay) — merchant-scoped
# ────────────────────────────────────────────────────────────────────
@router.get("/settlements")
async def settlements(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    limit:     int  = Query(50, ge=1, le=200),
    offset:    int  = Query(0,  ge=0),
    user: UserContext = Depends(require_permission("reports.read")),
):
    """
    List Razorpay settlements that funded this merchant.

    Reads from `rzp_settlements` filtered by `merchant_id`. Net amounts are
    what actually hit the merchant's bank account.
    """
    f, t = _window(from_date, to_date, default_days=29)
    f_ts = datetime.combine(f, datetime.min.time()).replace(tzinfo=timezone.utc)
    t_ts = datetime.combine(t, datetime.max.time()).replace(tzinfo=timezone.utc)
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT settlement_id, amount_paise, fees_paise, tax_paise,
                   utr, status, created_at, settled_at
            FROM rzp_settlements
            WHERE merchant_id = $1::uuid
              AND created_at >= $2::timestamptz
              AND created_at <= $3::timestamptz
            ORDER BY created_at DESC
            LIMIT $4 OFFSET $5
            """,
            _mid(user), f_ts, t_ts, int(limit), int(offset),
        )
    return {
        "from_date": f, "to_date": t,
        "limit": limit, "offset": offset, "count": len(rows),
        "items": [dict(r) for r in rows],
    }


# ────────────────────────────────────────────────────────────────────
# Invoice PDFs
# ────────────────────────────────────────────────────────────────────
@router.get("/invoice/{order_id}.pdf")
async def customer_tax_invoice_pdf(
    order_id: str,
    user: UserContext = Depends(require_permission("reports.read")),
):
    """Customer-facing tax invoice PDF for a single order."""
    try:
        pdf_bytes, filename = await render_customer_invoice(
            merchant_id=_mid(user), order_id=order_id,
        )
    except NotFoundError:
        raise
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/saas-invoice/{year}/{month}.pdf")
async def bittu_saas_invoice_pdf(
    year: int,
    month: int,
    currency: str = Query("INR", min_length=3, max_length=3),
    user: UserContext = Depends(require_permission("reports.read")),
):
    """
    Bittu's monthly SaaS / payment-processing invoice for this merchant.

    Idempotent: re-renders the same invoice on every call but persists a
    single row per (merchant, year, month) in `bittu_saas_invoices`.
    """
    if not (2020 <= year <= 2100):
        raise ValidationError("year out of range")
    if not (1 <= month <= 12):
        raise ValidationError("month must be 1..12")

    pdf_bytes, filename = await render_saas_invoice(
        merchant_id=_mid(user), year=year, month=month, currency=currency,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
