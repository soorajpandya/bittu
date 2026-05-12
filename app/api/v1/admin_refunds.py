"""
Refunds — Admin API (Phase 7).

Prefix:   /admin/refunds
Audience: platform admins. Cross-merchant access; merchant_id is an OPTIONAL
filter, not a constraint.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from app.core.auth import require_platform_admin
from app.services.refund_service import refund_service

router = APIRouter(prefix="/admin/refunds", tags=["Admin Refunds"])


class _AdminCreateBody(BaseModel):
    merchant_id: str
    payment_id: str
    amount: Decimal = Field(..., gt=0)
    kind: str = "partial"
    reason: Optional[str] = None
    order_id: Optional[str] = None
    customer_contact: Optional[str] = None
    notes: Optional[dict] = None


class _AdminTransitionBody(BaseModel):
    new_status: str
    gateway_refund_id: Optional[str] = None
    failure_reason: Optional[str] = None
    notes: Optional[dict] = None


@router.post("/")
async def admin_create_refund(
    body: _AdminCreateBody,
    admin = Depends(require_platform_admin()),
):
    return await refund_service.create(
        merchant_id=body.merchant_id,
        payment_id=body.payment_id,
        amount=body.amount,
        kind=body.kind,
        reason=body.reason,
        order_id=body.order_id,
        customer_contact=body.customer_contact,
        notes=body.notes,
        initiated_by_admin_id=getattr(admin, "user_id", None),
    )


@router.get("/")
async def admin_list_refunds(
    merchant_id: Optional[str] = Query(None),
    status:      Optional[str] = Query(None),
    payment_id:  Optional[str] = Query(None),
    order_id:    Optional[str] = Query(None),
    kind:        Optional[str] = Query(None),
    from_ts:     Optional[datetime] = Query(None),
    to_ts:       Optional[datetime] = Query(None),
    limit:       int = Query(100, ge=1, le=500),
    offset:      int = Query(0, ge=0),
    _ = Depends(require_platform_admin()),
):
    return await refund_service.list_refunds(
        merchant_id=merchant_id, status=status, payment_id=payment_id,
        order_id=order_id, kind=kind, from_ts=from_ts, to_ts=to_ts,
        limit=limit, offset=offset,
    )


@router.get("/csv")
async def admin_export_refunds_csv(
    merchant_id: Optional[str] = Query(None),
    status:      Optional[str] = Query(None),
    from_ts:     Optional[datetime] = Query(None),
    to_ts:       Optional[datetime] = Query(None),
    limit:       int = Query(500, ge=1, le=500),
    _ = Depends(require_platform_admin()),
):
    rows = await refund_service.list_refunds(
        merchant_id=merchant_id, status=status,
        from_ts=from_ts, to_ts=to_ts, limit=limit,
    )
    out = refund_service.to_csv(rows)
    return Response(
        content=out["body"],
        media_type=out["content_type"],
        headers={"Content-Disposition": f'attachment; filename="{out["filename"]}"'},
    )


@router.get("/{refund_id}")
async def admin_get_refund(
    refund_id: int,
    _ = Depends(require_platform_admin()),
):
    return await refund_service.get(refund_id)


@router.post("/{refund_id}/transition")
async def admin_transition_refund(
    refund_id: int,
    body: _AdminTransitionBody,
    admin = Depends(require_platform_admin()),
):
    return await refund_service.transition(
        refund_id,
        merchant_id=None,
        new_status=body.new_status,
        gateway_refund_id=body.gateway_refund_id,
        failure_reason=body.failure_reason,
        notes=body.notes,
        actor_admin_id=getattr(admin, "user_id", None),
    )
