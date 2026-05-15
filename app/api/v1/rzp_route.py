"""
Razorpay Route REST API (Phase 7 — linked accounts + transfers).

Prefix ``/razorpay-route`` deliberately avoids any clash with the legacy
``/razorpay`` namespace. All gateway side-effects funnel through
``rzp_route_service`` so idempotency keys and merchant resolution stay in
exactly one place.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.logging import get_logger
from app.services.razorpay.route_service import rzp_route_service

logger = get_logger(__name__)

router = APIRouter(prefix="/razorpay-route", tags=["Razorpay Route"])


def _mid(user: UserContext) -> str:
    if not user.restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    return str(user.restaurant_id)


# ── Models ────────────────────────────────────────────────────────────────


class ProvisionLinkedAccountIn(BaseModel):
    bank_account_number: Optional[str] = Field(
        None,
        description="Full account number — used in-memory only. Stored as last4+sha256.",
    )
    ifsc: Optional[str] = None
    beneficiary_name: Optional[str] = None
    reference_id: Optional[str] = None
    notes: Optional[dict[str, Any]] = None


class CreateTransferIn(BaseModel):
    razorpay_payment_id: str = Field(..., min_length=4)
    amount_paise: int = Field(..., ge=100)
    currency: str = Field("INR", min_length=3, max_length=3)
    on_hold: bool = False
    on_hold_until_epoch: Optional[int] = None
    notes: Optional[dict[str, Any]] = None


class ReverseTransferIn(BaseModel):
    amount_paise: Optional[int] = Field(None, ge=100)
    notes: Optional[dict[str, Any]] = None


# ── Linked account ────────────────────────────────────────────────────────


@router.get("/linked-account")
async def get_linked_account(
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    return await rzp_route_service.get_linked_account(merchant_id=_mid(user))


@router.post("/linked-account/provision")
async def provision_linked_account(
    body: ProvisionLinkedAccountIn = Body(default_factory=ProvisionLinkedAccountIn),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    """
    Idempotent: if a linked account already exists for this merchant we
    just resync state from Razorpay rather than creating a second one.
    """
    try:
        return await rzp_route_service.provision_linked_account(
            merchant_id=_mid(user),
            bank_account_number=body.bank_account_number,
            ifsc_override=body.ifsc,
            beneficiary_name_override=body.beneficiary_name,
            reference_id=body.reference_id,
            extra_notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/linked-account/sync")
async def sync_linked_account(
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    try:
        return await rzp_route_service.sync_linked_account(merchant_id=_mid(user))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ── Transfers ─────────────────────────────────────────────────────────────


@router.get("/transfers")
async def list_transfers(
    status: Optional[str] = Query(None, description="created|processed|reversed|failed"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    return await rzp_route_service.list_transfers(
        merchant_id=_mid(user), status=status, limit=limit, offset=offset,
    )


@router.get("/transfers/{transfer_id}")
async def get_transfer(
    transfer_id: str,
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    row = await rzp_route_service.get_transfer(
        merchant_id=_mid(user), transfer_id=transfer_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="transfer not found")
    return row


@router.post("/transfers")
async def create_transfer(
    body: CreateTransferIn,
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    try:
        return await rzp_route_service.create_transfer(
            merchant_id=_mid(user),
            razorpay_payment_id=body.razorpay_payment_id,
            amount_paise=body.amount_paise,
            currency=body.currency,
            on_hold=body.on_hold,
            on_hold_until_epoch=body.on_hold_until_epoch,
            notes=body.notes,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/transfers/{transfer_id}/reverse")
async def reverse_transfer(
    transfer_id: str,
    body: ReverseTransferIn = Body(default_factory=ReverseTransferIn),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    try:
        return await rzp_route_service.reverse_transfer(
            merchant_id=_mid(user),
            transfer_id=transfer_id,
            amount_paise=body.amount_paise,
            notes=body.notes,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/transfers/{transfer_id}/sync")
async def sync_transfer(
    transfer_id: str,
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    return await rzp_route_service.sync_transfer(
        merchant_id=_mid(user), transfer_id=transfer_id,
    )
