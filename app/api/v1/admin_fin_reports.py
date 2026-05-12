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
