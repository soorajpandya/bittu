"""Financial Reports — Merchant API (Phase 8). Prefix: /fin-reports.

Distinct from `/reports` (accounting trial-balance/P&L) and `/analytics`
(operational dashboards). This router exposes fintech analytics derived
from payments / refunds / disputes / merchant_ledger / bittu_settlements,
scoped to the caller's merchant.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response

from app.core.auth import UserContext, require_permission
from app.core.exceptions import ValidationError
from app.services.reporting_service import reporting_service

router = APIRouter(prefix="/fin-reports", tags=["Financial Reports (Phase 8)"])


def _mid(user: UserContext) -> str:
    if not user.restaurant_id:
        raise ValidationError("Active restaurant context is required.")
    return str(user.restaurant_id)


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
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    currency:  str = Query("INR", min_length=3, max_length=3),
    user: UserContext = Depends(require_permission("reports.read")),
):
    f, t = _default_window(from_date, to_date)
    return await reporting_service.pnl(
        merchant_id=_mid(user), from_date=f, to_date=t, currency=currency,
    )


@router.get("/pnl/csv")
async def pnl_csv(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    currency:  str = Query("INR", min_length=3, max_length=3),
    user: UserContext = Depends(require_permission("reports.export")),
):
    f, t = _default_window(from_date, to_date)
    row = await reporting_service.pnl(
        merchant_id=_mid(user), from_date=f, to_date=t, currency=currency,
    )
    out = reporting_service.dict_to_csv(row, filename=f"pnl_{f}_{t}.csv")
    return Response(
        content=out["body"],
        media_type=out["content_type"],
        headers={"Content-Disposition": f'attachment; filename="{out["filename"]}"'},
    )


@router.get("/settlements/summary")
async def settlement_summary(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("reports.read")),
):
    f, t = _default_window(from_date, to_date)
    return await reporting_service.settlement_summary(
        merchant_id=_mid(user), from_date=f, to_date=t,
    )


@router.get("/refunds/summary")
async def refund_summary(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    currency:  str = Query("INR", min_length=3, max_length=3),
    user: UserContext = Depends(require_permission("reports.read")),
):
    f, t = _default_window(from_date, to_date)
    return await reporting_service.refund_summary(
        merchant_id=_mid(user), from_date=f, to_date=t, currency=currency,
    )


@router.get("/disputes/summary")
async def dispute_summary(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    currency:  str = Query("INR", min_length=3, max_length=3),
    user: UserContext = Depends(require_permission("reports.read")),
):
    f, t = _default_window(from_date, to_date)
    return await reporting_service.dispute_summary(
        merchant_id=_mid(user), from_date=f, to_date=t, currency=currency,
    )


@router.get("/daily")
async def daily(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    currency:  str = Query("INR", min_length=3, max_length=3),
    user: UserContext = Depends(require_permission("reports.read")),
):
    f, t = _default_window(from_date, to_date)
    return await reporting_service.daily_rollups(
        merchant_id=_mid(user), from_date=f, to_date=t, currency=currency,
    )


@router.get("/daily/csv")
async def daily_csv(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    currency:  str = Query("INR", min_length=3, max_length=3),
    user: UserContext = Depends(require_permission("reports.export")),
):
    f, t = _default_window(from_date, to_date)
    rows = await reporting_service.daily_rollups(
        merchant_id=_mid(user), from_date=f, to_date=t, currency=currency,
    )
    out = reporting_service.to_csv(rows, filename=f"daily_rollups_{f}_{t}.csv")
    return Response(
        content=out["body"],
        media_type=out["content_type"],
        headers={"Content-Disposition": f'attachment; filename="{out["filename"]}"'},
    )


@router.get("/monthly")
async def monthly(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    currency:  str = Query("INR", min_length=3, max_length=3),
    user: UserContext = Depends(require_permission("reports.read")),
):
    f, t = _default_window(from_date, to_date)
    return await reporting_service.monthly_rollups(
        merchant_id=_mid(user), from_date=f, to_date=t, currency=currency,
    )
