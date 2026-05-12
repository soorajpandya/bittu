"""
Escrow Ledger API.

  GET  /escrow/balance               Held balance (+ open holds total)
  GET  /escrow/entries               Paginated, filterable history
  GET  /escrow/entries/{id}          Single entry detail
  GET  /escrow/consistency-check     Recompute balance vs running

  GET  /escrow/config                Per-merchant T+N hold config
  PUT  /escrow/config                Update hold_days / enabled (admin)

  GET  /escrow/due-for-release       Preview holds eligible for release
  POST /escrow/release-due           Run the release loop (admin / cron)

  POST /escrow/manual-adjustment     Admin DR/CR correction
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.services.escrow_service import escrow_service

router = APIRouter(prefix="/escrow", tags=["Escrow Ledger"])
logger = get_logger(__name__)


# ── Balance ────────────────────────────────────────────────────────────
@router.get("/balance")
async def get_balance(
    currency: str = Query("INR", min_length=3, max_length=3),
    user: UserContext = Depends(require_permission("escrow.read")),
):
    """Currently-held escrow balance for the caller's merchant + currency."""
    return await escrow_service.get_balance(user, currency=currency)


# ── List entries ───────────────────────────────────────────────────────
@router.get("/entries")
async def list_entries(
    transaction_type: Optional[str] = Query(None, description="Filter by txn type"),
    payment_id:       Optional[str] = Query(None),
    settlement_id:    Optional[str] = Query(None),
    order_id:         Optional[str] = Query(None),
    from_date:        Optional[datetime] = Query(None, description="ISO8601 inclusive"),
    to_date:          Optional[datetime] = Query(None, description="ISO8601 inclusive"),
    currency:         str = Query("INR", min_length=3, max_length=3),
    limit:            int = Query(50, ge=1, le=200),
    cursor:           Optional[str] = Query(None, description="Opaque keyset cursor"),
    user: UserContext = Depends(require_permission("escrow.read")),
):
    return await escrow_service.list_entries(
        user,
        transaction_type=transaction_type,
        payment_id=payment_id,
        settlement_id=settlement_id,
        order_id=order_id,
        from_date=from_date.isoformat() if from_date else None,
        to_date=to_date.isoformat() if to_date else None,
        currency=currency,
        limit=limit,
        cursor=cursor,
    )


# ── Single entry ───────────────────────────────────────────────────────
@router.get("/entries/{entry_id}")
async def get_entry(
    entry_id: str,
    user: UserContext = Depends(require_permission("escrow.read")),
):
    return await escrow_service.get_entry(user, entry_id)


# ── Consistency ────────────────────────────────────────────────────────
@router.get("/consistency-check")
async def consistency_check(
    currency: str = Query("INR", min_length=3, max_length=3),
    user: UserContext = Depends(require_permission("escrow.admin")),
):
    return await escrow_service.verify_consistency(user, currency=currency)


# ── Config ─────────────────────────────────────────────────────────────
class ConfigBody(BaseModel):
    hold_days: Optional[int]  = Field(None, ge=0, le=90)
    enabled:   Optional[bool] = Field(None)


@router.get("/config")
async def get_config(
    user: UserContext = Depends(require_permission("escrow.read")),
):
    if not user.restaurant_id:
        raise ValidationError("This endpoint requires an active restaurant context.")
    return await escrow_service.get_config(user.restaurant_id)


@router.put("/config")
async def set_config(
    body: ConfigBody = Body(...),
    user: UserContext = Depends(require_permission("escrow.admin")),
):
    if not user.restaurant_id:
        raise ValidationError("This endpoint requires an active restaurant context.")
    if body.hold_days is None and body.enabled is None:
        raise ValidationError("At least one of hold_days / enabled is required.")
    return await escrow_service.set_config(
        user.restaurant_id,
        hold_days=body.hold_days,
        enabled=body.enabled,
    )


# ── Cron: due-for-release preview + trigger ────────────────────────────
@router.get("/due-for-release")
async def due_for_release(
    limit: int = Query(100, ge=1, le=500),
    user: UserContext = Depends(require_permission("escrow.admin")),
):
    """
    Preview holds whose hold_until has elapsed and which have not yet been
    released.  Read-only — calling this does not release anything.
    """
    return {
        "items": await escrow_service.list_due_for_release(limit=limit),
    }


@router.post("/release-due")
async def release_due(
    limit: int = Query(100, ge=1, le=500),
    user: UserContext = Depends(require_permission("escrow.admin")),
):
    """
    Execute auto-release for due holds.  Intended for cron-driven invocation
    (HTTP-triggered tick or systemd timer hitting this endpoint with an
    admin token).  Each release runs in its own transaction; failures are
    logged but do not block subsequent releases.
    """
    return await escrow_service.release_due(limit=limit, actor_id=user.user_id)


# ── Manual adjustment (admin) ──────────────────────────────────────────
class ManualAdjustmentBody(BaseModel):
    amount: float          = Field(..., gt=0)
    direction: str         = Field(..., description="'credit' or 'debit'")
    currency: str          = Field("INR", min_length=3, max_length=3)
    reason: str            = Field(..., min_length=3, max_length=500)
    idempotency_key: str   = Field(..., min_length=8, max_length=128)
    metadata: dict         = Field(default_factory=dict)


@router.post("/manual-adjustment")
async def manual_adjustment(
    body: ManualAdjustmentBody = Body(...),
    user: UserContext = Depends(require_permission("escrow.admin")),
):
    """Manually credit or debit the escrow ledger for back-office corrections."""
    if body.direction not in ("credit", "debit"):
        raise ValidationError("direction must be 'credit' or 'debit'")

    metadata = {
        **(body.metadata or {}),
        "reason":     body.reason,
        "actor_id":   user.user_id,
        "actor_role": user.role,
    }
    return await escrow_service.post_entry(
        merchant_id=user.restaurant_id,
        branch_id=user.branch_id,
        transaction_type="escrow_adjustment",
        debit_amount=body.amount  if body.direction == "debit"  else 0,
        credit_amount=body.amount if body.direction == "credit" else 0,
        currency=body.currency,
        source_type="manual_adjustment",
        idempotency_key=body.idempotency_key,
        metadata=metadata,
        created_by=user.user_id,
    )
