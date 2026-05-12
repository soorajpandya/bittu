"""
Fee Engine v2 — Merchant API (Phase 10). Prefix: /fee-plans.

Read-only for merchants. They can view the active plan + its rules and
preview a fee for a hypothetical gross amount on their own merchant.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.exceptions import ValidationError
from app.services.fee_service import fee_service

router = APIRouter(prefix="/fee-plans", tags=["Fee Plans"])


def _mid(user: UserContext) -> str:
    if not user.restaurant_id:
        raise ValidationError("Active restaurant context is required.")
    return str(user.restaurant_id)


class _PreviewBody(BaseModel):
    gross:          Decimal = Field(..., ge=0)
    payment_method: Optional[str] = None
    order_source:   Optional[str] = None
    currency:       str = "INR"


@router.get("/active")
async def get_active_plan(
    user: UserContext = Depends(require_permission("fee_plans.read")),
):
    plan = await fee_service.resolve_plan(_mid(user))
    rules = await fee_service.list_rules(plan["id"])
    return {"plan": plan, "rules": rules}


@router.post("/preview")
async def preview_fee(
    body: _PreviewBody,
    user: UserContext = Depends(require_permission("fee_plans.read")),
):
    return await fee_service.preview_fee(
        _mid(user),
        gross=body.gross,
        payment_method=body.payment_method,
        order_source=body.order_source,
        currency=body.currency,
    )


@router.get("/computations")
async def list_my_computations(
    payment_id: Optional[str] = Query(None),
    limit:  int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("fee_plans.read")),
):
    return await fee_service.list_computations(
        merchant_id=_mid(user), payment_id=payment_id,
        limit=limit, offset=offset,
    )
