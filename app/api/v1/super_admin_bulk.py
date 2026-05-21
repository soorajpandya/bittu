"""
Super-admin bulk merchant operations.

Prefix:   /super-admin/bulk
Gating:   require_platform_admin()

  • POST /suspend     — suspend many merchants in one shot
  • POST /unsuspend   — unsuspend many merchants
  • POST /notes       — drop an admin note on many merchants
  • GET  /export-kyc  — download KYC roll-up as CSV
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_platform_admin
from app.services.super_admin import bulk_ops_service

router = APIRouter(prefix="/super-admin/bulk", tags=["Super Admin · Bulk Ops"])


class BulkSuspendBody(BaseModel):
    merchant_ids: list[str] = Field(..., min_length=1, max_length=500)
    reason: str = Field(..., min_length=3, max_length=1000)


class BulkUnsuspendBody(BaseModel):
    merchant_ids: list[str] = Field(..., min_length=1, max_length=500)
    reason: Optional[str] = Field(default=None, max_length=1000)


class BulkNoteBody(BaseModel):
    merchant_ids: list[str] = Field(..., min_length=1, max_length=500)
    note: str = Field(..., min_length=1, max_length=4000)


@router.post("/suspend")
async def bulk_suspend(
    body: BulkSuspendBody,
    actor: UserContext = Depends(require_platform_admin()),
):
    try:
        return await bulk_ops_service.bulk_suspend(
            merchant_ids=body.merchant_ids,
            reason=body.reason,
            actor_id=actor.user_id,
            actor_email=actor.email,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/unsuspend")
async def bulk_unsuspend(
    body: BulkUnsuspendBody,
    actor: UserContext = Depends(require_platform_admin()),
):
    try:
        return await bulk_ops_service.bulk_unsuspend(
            merchant_ids=body.merchant_ids,
            reason=body.reason,
            actor_id=actor.user_id,
            actor_email=actor.email,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/notes")
async def bulk_add_note(
    body: BulkNoteBody,
    actor: UserContext = Depends(require_platform_admin()),
):
    try:
        return await bulk_ops_service.bulk_add_note(
            merchant_ids=body.merchant_ids,
            note=body.note,
            actor_id=actor.user_id,
            actor_email=actor.email,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/export-kyc")
async def export_kyc(
    status: Optional[str] = Query(default=None, max_length=32),
    limit: int = Query(default=5000, ge=1, le=20000),
    _: UserContext = Depends(require_platform_admin()),
):
    csv_text, count = await bulk_ops_service.export_kyc_csv(
        status=status, limit=limit,
    )

    def _stream():
        yield csv_text

    headers = {
        "Content-Disposition": f'attachment; filename="kyc_export_{count}.csv"',
        "X-Row-Count":         str(count),
    }
    return StreamingResponse(_stream(), media_type="text/csv", headers=headers)
