"""
Super-admin platform-finance rollups.

Prefix:   /super-admin/finance
Gating:   require_platform_admin()

  • GET /pnl                 — settlements + payments + refunds + disputes
  • GET /fee-revenue         — bittu_settlements rollup
  • GET /gst                 — GST collected, by day
  • GET /tpv                 — total payment volume by day
  • GET /refund-liability    — current refund obligation
  • GET /dispute-exposure    — open dispute amount
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import UserContext, require_platform_admin
from app.services.super_admin import finance_service

router = APIRouter(prefix="/super-admin/finance", tags=["Super Admin · Finance"])


@router.get("/pnl")
async def pnl(
    from_date: Optional[date] = Query(default=None, alias="from"),
    to_date: Optional[date] = Query(default=None, alias="to"),
    _: UserContext = Depends(require_platform_admin()),
):
    try:
        return await finance_service.pnl(from_date=from_date, to_date=to_date)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/fee-revenue")
async def fee_revenue(
    from_date: Optional[date] = Query(default=None, alias="from"),
    to_date: Optional[date] = Query(default=None, alias="to"),
    _: UserContext = Depends(require_platform_admin()),
):
    try:
        return await finance_service.fee_revenue(from_date=from_date, to_date=to_date)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/gst")
async def gst(
    from_date: Optional[date] = Query(default=None, alias="from"),
    to_date: Optional[date] = Query(default=None, alias="to"),
    _: UserContext = Depends(require_platform_admin()),
):
    try:
        return await finance_service.gst_collected(from_date=from_date, to_date=to_date)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/tpv")
async def tpv(
    from_date: Optional[date] = Query(default=None, alias="from"),
    to_date: Optional[date] = Query(default=None, alias="to"),
    _: UserContext = Depends(require_platform_admin()),
):
    try:
        return await finance_service.tpv(from_date=from_date, to_date=to_date)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/refund-liability")
async def refund_liability(
    _: UserContext = Depends(require_platform_admin()),
):
    return await finance_service.refund_liability()


@router.get("/dispute-exposure")
async def dispute_exposure(
    _: UserContext = Depends(require_platform_admin()),
):
    return await finance_service.dispute_exposure()
