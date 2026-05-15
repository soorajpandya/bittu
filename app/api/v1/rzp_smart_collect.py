"""
Razorpay Smart Collect REST API (Phase 8 — virtual accounts).

Prefix ``/razorpay-smart-collect``. All gateway side-effects funnel
through ``rzp_smart_collect_service`` so idempotency keys and merchant
resolution stay in exactly one place.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.logging import get_logger
from app.services.razorpay.smart_collect_service import rzp_smart_collect_service

logger = get_logger(__name__)

router = APIRouter(prefix="/razorpay-smart-collect", tags=["Razorpay Smart Collect"])


def _mid(user: UserContext) -> str:
    if not user.restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    return str(user.restaurant_id)


def _bid(user: UserContext) -> Optional[str]:
    bid = getattr(user, "branch_id", None)
    return str(bid) if bid else None


# ── Models ────────────────────────────────────────────────────────────────


class ProvisionVAIn(BaseModel):
    receivers_types: list[str] = Field(
        ..., min_length=1,
        description='Subset of ["bank_account","vpa"]',
    )
    descriptor: Optional[str] = Field(
        None, description="Custom UPI handle suffix (when 'vpa' in receivers_types)",
    )
    customer_id: Optional[str] = None
    description: Optional[str] = None
    amount_expected_paise: Optional[int] = Field(None, ge=100)
    notes: Optional[dict[str, Any]] = None
    allowed_payers: Optional[list[dict[str, Any]]] = None
    close_by_epoch: Optional[int] = Field(
        None, description="Unix epoch at which VA auto-closes",
    )


class AddPayerIn(BaseModel):
    payer: dict[str, Any] = Field(
        ..., description='Razorpay-shape allowed_payer dict (e.g. {"type":"bank_account","bank_account":{...}})',
    )


# ── Virtual accounts ──────────────────────────────────────────────────────


@router.get("/virtual-accounts")
async def list_virtual_accounts(
    status: Optional[str] = Query(None, regex="^(active|closed)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("razorpay.smart_collect.read")),
):
    return await rzp_smart_collect_service.list_virtual_accounts(
        merchant_id=_mid(user), status=status, limit=limit, offset=offset,
    )


@router.get("/virtual-accounts/{virtual_account_id}")
async def get_virtual_account(
    virtual_account_id: str,
    user: UserContext = Depends(require_permission("razorpay.smart_collect.read")),
):
    row = await rzp_smart_collect_service.get_virtual_account(
        merchant_id=_mid(user), virtual_account_id=virtual_account_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="virtual_account_not_found")
    return row


@router.post("/virtual-accounts")
async def provision_virtual_account(
    body: ProvisionVAIn,
    user: UserContext = Depends(require_permission("razorpay.smart_collect.write")),
):
    try:
        return await rzp_smart_collect_service.provision_virtual_account(
            merchant_id=_mid(user),
            branch_id=_bid(user),
            receivers_types=body.receivers_types,
            descriptor=body.descriptor,
            customer_id=body.customer_id,
            description=body.description,
            amount_expected_paise=body.amount_expected_paise,
            notes=body.notes,
            allowed_payers=body.allowed_payers,
            close_by_epoch=body.close_by_epoch,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/virtual-accounts/{virtual_account_id}/close")
async def close_virtual_account(
    virtual_account_id: str,
    user: UserContext = Depends(require_permission("razorpay.smart_collect.write")),
):
    try:
        return await rzp_smart_collect_service.close_virtual_account(
            merchant_id=_mid(user), virtual_account_id=virtual_account_id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="virtual_account_not_found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")


@router.post("/virtual-accounts/{virtual_account_id}/sync")
async def sync_virtual_account(
    virtual_account_id: str,
    user: UserContext = Depends(require_permission("razorpay.smart_collect.read")),
):
    try:
        return await rzp_smart_collect_service.sync_virtual_account(
            merchant_id=_mid(user), virtual_account_id=virtual_account_id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="virtual_account_not_found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")


@router.post("/virtual-accounts/{virtual_account_id}/payments/sync")
async def sync_va_payments(
    virtual_account_id: str,
    count: int = Query(25, ge=1, le=100),
    user: UserContext = Depends(require_permission("razorpay.smart_collect.read")),
):
    try:
        return await rzp_smart_collect_service.sync_va_payments(
            merchant_id=_mid(user),
            virtual_account_id=virtual_account_id,
            count=count,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="virtual_account_not_found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")


@router.post("/virtual-accounts/{virtual_account_id}/allowed-payers")
async def add_allowed_payer(
    virtual_account_id: str,
    body: AddPayerIn,
    user: UserContext = Depends(require_permission("razorpay.smart_collect.write")),
):
    try:
        return await rzp_smart_collect_service.add_allowed_payer(
            merchant_id=_mid(user),
            virtual_account_id=virtual_account_id,
            payer=body.payer,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="virtual_account_not_found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── Transactions (inbound credits) ────────────────────────────────────────


@router.get("/transactions")
async def list_transactions(
    virtual_account_id: Optional[str] = Query(None),
    reconciled: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("razorpay.smart_collect.read")),
):
    return await rzp_smart_collect_service.list_transactions(
        merchant_id=_mid(user),
        virtual_account_id=virtual_account_id,
        reconciled=reconciled,
        limit=limit,
        offset=offset,
    )


__all__ = ["router"]
