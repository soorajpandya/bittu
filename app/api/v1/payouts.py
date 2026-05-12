"""
Payout / Disbursement — Merchant API (Phase 4).

Prefix:   /payouts
Audience: a merchant initiating payouts from their own balance.
All endpoints scope to ``user.restaurant_id`` — a merchant CANNOT see
or affect another merchant's payouts through this router.

Use the platform-admin router at ``/admin/payouts`` for cross-merchant
operations: approval, batching, file generation, mark-sent / failed.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.services.payout_service import payout_service

router = APIRouter(prefix="/payouts", tags=["Payouts"])
logger = get_logger(__name__)


def _merchant_id(user: UserContext) -> str:
    if not user.restaurant_id:
        raise ValidationError("Active restaurant context is required.")
    return str(user.restaurant_id)


# ── Schemas ──────────────────────────────────────────────────────────────
class BeneficiaryCreate(BaseModel):
    label:           str = Field(..., min_length=2, max_length=100)
    type:            str = Field(..., pattern=r"^(bank|upi)$")
    account_holder:  Optional[str] = None
    account_number:  Optional[str] = None
    ifsc:            Optional[str] = None
    bank_name:       Optional[str] = None
    upi_vpa:         Optional[str] = None
    metadata:        Optional[dict] = None


class PayoutRequestBody(BaseModel):
    beneficiary_id:  str
    amount:          float = Field(..., gt=0)
    method:          str = Field("bank_neft", pattern=r"^(bank_neft|bank_imps|bank_rtgs|upi)$")
    currency:        str = Field("INR", min_length=3, max_length=3)
    branch_id:       Optional[str] = None
    notes:           Optional[str] = None
    idempotency_key: Optional[str] = Field(None, max_length=120)
    metadata:        Optional[dict] = None


class CancelBody(BaseModel):
    notes: Optional[str] = None


# ── Beneficiaries ────────────────────────────────────────────────────────
@router.get("/beneficiaries")
async def list_beneficiaries(
    only_active: bool = Query(True),
    user: UserContext = Depends(require_permission("payout.read")),
):
    return await payout_service.list_beneficiaries(
        merchant_id=_merchant_id(user), only_active=only_active,
    )


@router.post("/beneficiaries", status_code=201)
async def create_beneficiary(
    body: BeneficiaryCreate,
    user: UserContext = Depends(require_permission("payout.write")),
):
    return await payout_service.create_beneficiary(
        merchant_id=_merchant_id(user),
        label=body.label, type=body.type,
        account_holder=body.account_holder,
        account_number=body.account_number,
        ifsc=body.ifsc, bank_name=body.bank_name,
        upi_vpa=body.upi_vpa, metadata=body.metadata,
        created_by=user.user_id,
    )


@router.get("/beneficiaries/{beneficiary_id}")
async def get_beneficiary(
    beneficiary_id: str,
    user: UserContext = Depends(require_permission("payout.read")),
):
    return await payout_service.get_beneficiary(
        beneficiary_id=beneficiary_id, merchant_id=_merchant_id(user),
    )


@router.delete("/beneficiaries/{beneficiary_id}")
async def deactivate_beneficiary(
    beneficiary_id: str,
    user: UserContext = Depends(require_permission("payout.write")),
):
    return await payout_service.deactivate_beneficiary(
        beneficiary_id=beneficiary_id, merchant_id=_merchant_id(user),
    )


# ── Available balance ────────────────────────────────────────────────────
@router.get("/available-balance")
async def available_balance(
    currency: str = Query("INR", min_length=3, max_length=3),
    user: UserContext = Depends(require_permission("payout.read")),
):
    return await payout_service.available_balance(
        merchant_id=_merchant_id(user), currency=currency,
    )


# ── Payout requests ──────────────────────────────────────────────────────
@router.post("/", status_code=201)
async def request_payout(
    body: PayoutRequestBody,
    user: UserContext = Depends(require_permission("payout.write")),
):
    return await payout_service.request_payout(
        merchant_id=_merchant_id(user),
        beneficiary_id=body.beneficiary_id,
        amount=body.amount, method=body.method, currency=body.currency,
        branch_id=body.branch_id, requested_by=user.user_id,
        notes=body.notes, idempotency_key=body.idempotency_key,
        metadata=body.metadata,
    )


@router.get("/")
async def list_payouts(
    status: Optional[str] = Query(None),
    beneficiary_id: Optional[str] = Query(None),
    from_date: Optional[datetime] = Query(None),
    to_date:   Optional[datetime] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    user: UserContext = Depends(require_permission("payout.read")),
):
    return await payout_service.list_payouts(
        merchant_id=_merchant_id(user), status=status,
        beneficiary_id=beneficiary_id,
        from_date=from_date, to_date=to_date, limit=limit,
    )


@router.get("/summary")
async def summary(
    user: UserContext = Depends(require_permission("payout.read")),
):
    return await payout_service.get_summary(merchant_id=_merchant_id(user))


@router.get("/{payout_id}")
async def get_payout(
    payout_id: str,
    user: UserContext = Depends(require_permission("payout.read")),
):
    return await payout_service.get_payout(
        payout_id=payout_id, merchant_id=_merchant_id(user),
    )


@router.get("/{payout_id}/events")
async def list_events(
    payout_id: str,
    user: UserContext = Depends(require_permission("payout.read")),
):
    # Verify ownership before exposing events
    await payout_service.get_payout(
        payout_id=payout_id, merchant_id=_merchant_id(user),
    )
    return await payout_service.list_events(payout_id)


@router.post("/{payout_id}/cancel")
async def cancel_payout(
    payout_id: str,
    body: CancelBody = Body(default_factory=CancelBody),
    user: UserContext = Depends(require_permission("payout.write")),
):
    return await payout_service.cancel_payout(
        payout_id=payout_id, merchant_id=_merchant_id(user),
        actor_id=user.user_id, notes=body.notes,
    )
