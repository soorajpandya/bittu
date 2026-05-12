"""
Fee Engine v2 — Admin API (Phase 10). Prefix: /admin/fee-plans.

Cross-merchant. Plans, rules, per-merchant overrides, and the
computations audit log. All routes require platform admin.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_platform_admin
from app.services.fee_service import fee_service

router = APIRouter(prefix="/admin/fee-plans", tags=["Fee Plans (Admin)"])


# ──────────────────────────── pydantic ───────────────────────────────
class _PlanCreate(BaseModel):
    code:        str
    name:        str
    description: Optional[str] = None
    currency:    str = "INR"
    gst_rate:    Decimal = Field(Decimal("0.18"), ge=0, le=1)
    is_active:   bool = True
    is_default:  bool = False
    valid_from:  Optional[str] = None
    valid_to:    Optional[str] = None
    metadata:    Optional[dict] = None


class _PlanUpdate(BaseModel):
    name:        Optional[str] = None
    description: Optional[str] = None
    gst_rate:    Optional[Decimal] = Field(None, ge=0, le=1)
    is_active:   Optional[bool] = None
    is_default:  Optional[bool] = None
    valid_to:    Optional[str] = None
    metadata:    Optional[dict] = None


class _RuleCreate(BaseModel):
    payment_method: Optional[str] = None
    order_source:   Optional[str] = None
    min_amount:     Decimal = Field(Decimal("0"), ge=0)
    max_amount:     Optional[Decimal] = None
    fee_type:       str = "percent"
    percent_rate:   Decimal = Field(Decimal("0"), ge=0, le=1)
    flat_fee:       Decimal = Field(Decimal("0"), ge=0)
    priority:       int = 100
    is_active:      bool = True
    metadata:       Optional[dict] = None


class _RuleUpdate(BaseModel):
    payment_method: Optional[str] = None
    order_source:   Optional[str] = None
    min_amount:     Optional[Decimal] = Field(None, ge=0)
    max_amount:     Optional[Decimal] = None
    fee_type:       Optional[str] = None
    percent_rate:   Optional[Decimal] = Field(None, ge=0, le=1)
    flat_fee:       Optional[Decimal] = Field(None, ge=0)
    priority:       Optional[int] = None
    is_active:      Optional[bool] = None
    metadata:       Optional[dict] = None


class _OverrideBody(BaseModel):
    plan_id:    int
    valid_from: Optional[str] = None
    valid_to:   Optional[str] = None
    reason:     Optional[str] = None
    metadata:   Optional[dict] = None


class _ComputeBody(BaseModel):
    merchant_id:    str
    gross:          Decimal = Field(..., ge=0)
    payment_method: Optional[str] = None
    order_source:   Optional[str] = None
    currency:       str = "INR"
    record:         bool = False
    payment_id:     Optional[str] = None


# ╔══════════════════════════ plans ══════════════════════════════════╗
@router.get("/plans")
async def list_plans(
    active_only: bool = Query(False),
    limit:  int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await fee_service.list_plans(
        active_only=active_only, limit=limit, offset=offset
    )


@router.post("/plans")
async def create_plan(
    body: _PlanCreate,
    admin: UserContext = Depends(require_platform_admin()),
):
    return await fee_service.create_plan(
        **body.model_dump(exclude_none=True),
        created_by_admin_id=admin.user_id,
    )


@router.get("/plans/{plan_id}")
async def get_plan(
    plan_id: int = Path(..., ge=1),
    admin: UserContext = Depends(require_platform_admin()),
):
    plan = await fee_service.get_plan(plan_id)
    rules = await fee_service.list_rules(plan_id)
    return {"plan": plan, "rules": rules}


@router.patch("/plans/{plan_id}")
async def update_plan(
    body: _PlanUpdate,
    plan_id: int = Path(..., ge=1),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await fee_service.update_plan(
        plan_id, **body.model_dump(exclude_none=True)
    )


# ╔══════════════════════════ rules ══════════════════════════════════╗
@router.get("/plans/{plan_id}/rules")
async def list_rules(
    plan_id: int = Path(..., ge=1),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await fee_service.list_rules(plan_id)


@router.post("/plans/{plan_id}/rules")
async def add_rule(
    body: _RuleCreate,
    plan_id: int = Path(..., ge=1),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await fee_service.add_rule(
        plan_id, **body.model_dump(exclude_none=True)
    )


@router.patch("/rules/{rule_id}")
async def update_rule(
    body: _RuleUpdate,
    rule_id: int = Path(..., ge=1),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await fee_service.update_rule(
        rule_id, **body.model_dump(exclude_none=True)
    )


@router.delete("/rules/{rule_id}")
async def delete_rule(
    rule_id: int = Path(..., ge=1),
    admin: UserContext = Depends(require_platform_admin()),
):
    await fee_service.remove_rule(rule_id)
    return {"deleted": True, "rule_id": rule_id}


# ╔══════════════════════════ overrides ══════════════════════════════╗
@router.get("/overrides")
async def list_overrides(
    merchant_id: Optional[str] = Query(None),
    active_only: bool = Query(False),
    limit:  int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await fee_service.list_overrides(
        merchant_id=merchant_id, active_only=active_only,
        limit=limit, offset=offset,
    )


@router.post("/overrides/{merchant_id}")
async def set_override(
    body: _OverrideBody,
    merchant_id: str = Path(...),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await fee_service.set_merchant_override(
        merchant_id,
        plan_id=body.plan_id, valid_from=body.valid_from,
        valid_to=body.valid_to, reason=body.reason,
        metadata=body.metadata,
        created_by_admin_id=admin.user_id,
    )


@router.post("/overrides/{override_id}/end")
async def end_override(
    override_id: int = Path(..., ge=1),
    valid_to: Optional[str] = Query(None),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await fee_service.end_override(override_id, valid_to=valid_to)


# ╔══════════════════════════ resolve / compute ══════════════════════╗
@router.get("/resolve/{merchant_id}")
async def resolve_plan(
    merchant_id: str = Path(...),
    at_ts: Optional[str] = Query(None),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await fee_service.resolve_plan(merchant_id, at_ts=at_ts)


@router.post("/compute")
async def compute_fee(
    body: _ComputeBody,
    admin: UserContext = Depends(require_platform_admin()),
):
    return await fee_service.compute_fee(
        body.merchant_id,
        gross=body.gross,
        payment_method=body.payment_method,
        order_source=body.order_source,
        currency=body.currency,
        record=body.record,
        payment_id=body.payment_id,
    )


# ╔══════════════════════════ computations log ═══════════════════════╗
@router.get("/computations")
async def list_computations(
    merchant_id: Optional[str] = Query(None),
    payment_id:  Optional[str] = Query(None),
    limit:  int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await fee_service.list_computations(
        merchant_id=merchant_id, payment_id=payment_id,
        limit=limit, offset=offset,
    )
