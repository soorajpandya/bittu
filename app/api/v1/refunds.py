"""Refunds — Merchant API (Phase 7). Prefix: /refunds. Scoped to caller's merchant."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response, Body
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.exceptions import ValidationError
from app.services.refund_service import refund_service

router = APIRouter(prefix="/refunds", tags=["Refunds"])


def _mid(user: UserContext) -> str:
    if not user.restaurant_id:
        raise ValidationError("Active restaurant context is required.")
    return str(user.restaurant_id)


class _CreateBody(BaseModel):
    payment_id: str
    amount: Decimal = Field(..., gt=0)
    kind: str = "partial"
    reason: Optional[str] = None
    order_id: Optional[str] = None
    customer_contact: Optional[str] = None
    notes: Optional[dict] = None
    speed: str = "normal"  # razorpay: normal|optimum (ignored for non-online payments)


class _TransitionBody(BaseModel):
    new_status: str
    gateway_refund_id: Optional[str] = None
    failure_reason: Optional[str] = None
    notes: Optional[dict] = None


@router.post("/")
async def create_refund(
    body: _CreateBody,
    user: UserContext = Depends(require_permission("refunds.write")),
):
    return await refund_service.create_and_dispatch(
        merchant_id=_mid(user),
        payment_id=body.payment_id,
        amount=body.amount,
        kind=body.kind,
        reason=body.reason,
        order_id=body.order_id,
        customer_contact=body.customer_contact,
        notes=body.notes,
        speed=body.speed,
        initiated_by_user_id=user.user_id,
    )


@router.get("/")
async def list_refunds(
    status:     Optional[str] = Query(None),
    payment_id: Optional[str] = Query(None),
    order_id:   Optional[str] = Query(None),
    kind:       Optional[str] = Query(None),
    from_ts:    Optional[datetime] = Query(None),
    to_ts:      Optional[datetime] = Query(None),
    limit:      int = Query(100, ge=1, le=500),
    offset:     int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("refunds.read")),
):
    return await refund_service.list_refunds(
        merchant_id=_mid(user),
        status=status, payment_id=payment_id, order_id=order_id, kind=kind,
        from_ts=from_ts, to_ts=to_ts, limit=limit, offset=offset,
    )


@router.get("/csv")
async def export_refunds_csv(
    status:     Optional[str] = Query(None),
    from_ts:    Optional[datetime] = Query(None),
    to_ts:      Optional[datetime] = Query(None),
    limit:      int = Query(500, ge=1, le=500),
    user: UserContext = Depends(require_permission("refunds.read")),
):
    rows = await refund_service.list_refunds(
        merchant_id=_mid(user), status=status, from_ts=from_ts, to_ts=to_ts, limit=limit,
    )
    out = refund_service.to_csv(rows)
    return Response(
        content=out["body"],
        media_type=out["content_type"],
        headers={"Content-Disposition": f'attachment; filename="{out["filename"]}"'},
    )


@router.get("/refundable/{payment_id}")
async def refundable_amount(
    payment_id: str,
    user: UserContext = Depends(require_permission("refunds.read")),
):
    amt = await refund_service.refundable_amount(
        merchant_id=_mid(user), payment_id=payment_id,
    )
    return {"payment_id": payment_id, "refundable_amount": str(amt)}


@router.get("/{refund_id}")
async def get_refund(
    refund_id: int,
    user: UserContext = Depends(require_permission("refunds.read")),
):
    return await refund_service.get(refund_id, merchant_id=_mid(user))


@router.post("/{refund_id}/transition")
async def transition_refund(
    refund_id: int,
    body: _TransitionBody,
    user: UserContext = Depends(require_permission("refunds.write")),
):
    return await refund_service.transition(
        refund_id,
        merchant_id=_mid(user),
        new_status=body.new_status,
        gateway_refund_id=body.gateway_refund_id,
        failure_reason=body.failure_reason,
        notes=body.notes,
        actor_user_id=user.user_id,
    )
