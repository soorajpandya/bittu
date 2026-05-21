"""
Super-admin reconciliation engine controls (Phase 9).

Prefix:   /super-admin/reconciliation
Gating:   require_platform_admin()

  GET  /runs                       — recent reconciliation runs
  GET  /runs/{run_id}              — single run detail
  POST /run-now                    — trigger a manual reconciliation pass
  GET  /discrepancies              — filterable discrepancy list
  PATCH /discrepancies/{id}        — change discrepancy status / add note
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_platform_admin
from app.core.logging import get_logger
from app.services.razorpay.reconciliation import rzp_reconciliation_service

router = APIRouter(
    prefix="/super-admin/reconciliation",
    tags=["Super Admin · Reconciliation"],
)
logger = get_logger(__name__)


class RunNowRequest(BaseModel):
    window_from: Optional[datetime] = None
    window_to:   Optional[datetime] = None


class UpdateDiscrepancyRequest(BaseModel):
    status: str = Field(..., pattern=r"^(open|investigating|resolved|ignored)$")
    resolution_note: Optional[str] = Field(default=None, max_length=2000)


@router.get("/runs")
async def list_runs(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: UserContext = Depends(require_platform_admin()),
):
    items = await rzp_reconciliation_service.list_runs(limit=limit, offset=offset)
    return {"items": items, "count": len(items)}


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str = Path(..., min_length=8, max_length=64),
    _: UserContext = Depends(require_platform_admin()),
):
    row = await rzp_reconciliation_service.get_run(run_id)
    if row is None:
        raise HTTPException(404, "run not found")
    return row


@router.post("/run-now")
async def run_now(
    payload: RunNowRequest = Body(default_factory=RunNowRequest),
    actor: UserContext = Depends(require_platform_admin()),
):
    try:
        result = await rzp_reconciliation_service.run_daily_reconciliation(
            window_from=payload.window_from,
            window_to=payload.window_to,
            triggered_by="manual",
            actor_user_id=actor.user_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("rzp_recon_manual_run_failed")
        raise HTTPException(500, f"reconciliation_failed: {exc}")
    return result


@router.get("/discrepancies")
async def list_discrepancies(
    run_id: Optional[str] = Query(default=None, max_length=64),
    discrepancy_type: Optional[str] = Query(default=None, max_length=64),
    status: Optional[str] = Query(
        default=None, pattern=r"^(open|investigating|resolved|ignored)$",
    ),
    merchant_id: Optional[str] = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: UserContext = Depends(require_platform_admin()),
):
    items = await rzp_reconciliation_service.list_discrepancies(
        run_id=run_id,
        discrepancy_type=discrepancy_type,
        status=status,
        merchant_id=merchant_id,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "count": len(items)}


@router.patch("/discrepancies/{discrepancy_id}")
async def update_discrepancy(
    discrepancy_id: str = Path(..., min_length=8, max_length=64),
    payload: UpdateDiscrepancyRequest = Body(...),
    actor: UserContext = Depends(require_platform_admin()),
):
    try:
        row = await rzp_reconciliation_service.update_discrepancy_status(
            discrepancy_id=discrepancy_id,
            new_status=payload.status,
            resolved_by_user_id=actor.user_id,
            resolution_note=payload.resolution_note,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if row is None:
        raise HTTPException(404, "discrepancy not found")
    return row
