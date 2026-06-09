"""
Super-admin Razorpay Route oversight.

Prefix:   /super-admin/route
Gating:   require_platform_admin()

Distinct from the per-merchant `/razorpay-route/*` surface:
  • Lists/searches linked accounts across ALL merchants.
  • Lets ops force-sync or force-onboard a specific merchant.
  • Surfaces an onboarding triage queue grouped by funnel state.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_platform_admin
from app.services.super_admin import route_admin_service

router = APIRouter(
    prefix="/super-admin/route", tags=["Super Admin · Route"],
)


@router.get("/accounts")
async def list_linked_accounts(
    status: Optional[str] = Query(default=None, max_length=32),
    kyc_status: Optional[str] = Query(default=None, max_length=64),
    route_product_status: Optional[str] = Query(default=None, max_length=64),
    search: Optional[str] = Query(default=None, max_length=256),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: UserContext = Depends(require_platform_admin()),
):
    return await route_admin_service.list_linked_accounts(
        status=status,
        kyc_status=kyc_status,
        route_product_status=route_product_status,
        search=search,
        limit=limit, offset=offset,
    )


@router.get("/accounts/{merchant_id}")
async def get_linked_account(
    merchant_id: str = Path(..., min_length=8, max_length=64),
    _: UserContext = Depends(require_platform_admin()),
):
    try:
        return await route_admin_service.get_linked_account_full(merchant_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))


@router.post("/accounts/{merchant_id}/sync")
async def sync_linked_account(
    merchant_id: str = Path(..., min_length=8, max_length=64),
    _: UserContext = Depends(require_platform_admin()),
):
    try:
        return await route_admin_service.force_sync_linked_account(merchant_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/accounts/{merchant_id}/sync-product")
async def sync_route_product(
    merchant_id: str = Path(..., min_length=8, max_length=64),
    _: UserContext = Depends(require_platform_admin()),
):
    try:
        return await route_admin_service.force_sync_product(merchant_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class OnboardBody(BaseModel):
    bank_account_number: str = Field(..., min_length=4, max_length=32)
    ifsc: Optional[str] = Field(default=None, max_length=16)
    beneficiary_name: Optional[str] = Field(default=None, max_length=200)
    reference_id: Optional[str] = Field(default=None, max_length=64)
    tnc_accepted: bool = True
    extra_notes: Optional[dict] = None


@router.post("/accounts/{merchant_id}/onboard")
async def force_onboard(
    merchant_id: str = Path(..., min_length=8, max_length=64),
    body: OnboardBody = ...,
    _: UserContext = Depends(require_platform_admin()),
):
    try:
        return await route_admin_service.force_onboard(
            merchant_id=merchant_id,
            bank_account_number=body.bank_account_number,
            ifsc=body.ifsc,
            beneficiary_name=body.beneficiary_name,
            reference_id=body.reference_id,
            tnc_accepted=body.tnc_accepted,
            extra_notes=body.extra_notes,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except PermissionError as exc:
        raise HTTPException(403, str(exc))


@router.get("/transfers")
async def list_transfers(
    merchant_id: Optional[str] = Query(default=None, max_length=64),
    status: Optional[str] = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: UserContext = Depends(require_platform_admin()),
):
    return await route_admin_service.list_transfers(
        merchant_id=merchant_id, status=status, limit=limit, offset=offset,
    )


@router.get("/onboarding-queue")
async def onboarding_queue(
    limit: int = Query(default=100, ge=1, le=500),
    _: UserContext = Depends(require_platform_admin()),
):
    return await route_admin_service.onboarding_queue(limit=limit)


class BackfillAccountBody(BaseModel):
    linked_account_id: str = Field(..., min_length=4, max_length=64)
    status: str = Field(default="activated", max_length=16)
    kyc_status: Optional[str] = Field(default="activated", max_length=64)
    activation_status: Optional[str] = Field(default="activated", max_length=64)
    route_product_status: Optional[str] = Field(default="activated", max_length=64)
    route_product_id: Optional[str] = Field(default=None, max_length=64)
    stakeholder_id: Optional[str] = Field(default=None, max_length=64)
    legal_business_name: Optional[str] = Field(default=None, max_length=200)
    business_type: Optional[str] = Field(default=None, max_length=64)
    contact_name: Optional[str] = Field(default=None, max_length=200)
    email: Optional[str] = Field(default=None, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=20)
    reference_id: Optional[str] = Field(default=None, max_length=64)
    bank_account_ifsc: Optional[str] = Field(default=None, max_length=16)
    bank_account_last4: Optional[str] = Field(default=None, max_length=4)
    tnc_accepted: bool = True
    notes: Optional[dict] = None


@router.post("/accounts/{merchant_id}/backfill")
async def backfill_linked_account(
    merchant_id: str = Path(..., min_length=8, max_length=64),
    body: BackfillAccountBody = ...,
    _: UserContext = Depends(require_platform_admin()),
):
    """Seed or correct a merchant's rzp_route_accounts row (no Razorpay call)."""
    try:
        return await route_admin_service.backfill_linked_account(
            merchant_id=merchant_id,
            linked_account_id=body.linked_account_id,
            status=body.status,
            kyc_status=body.kyc_status,
            activation_status=body.activation_status,
            route_product_status=body.route_product_status,
            route_product_id=body.route_product_id,
            stakeholder_id=body.stakeholder_id,
            legal_business_name=body.legal_business_name,
            business_type=body.business_type,
            contact_name=body.contact_name,
            email=body.email,
            phone=body.phone,
            reference_id=body.reference_id,
            bank_account_ifsc=body.bank_account_ifsc,
            bank_account_last4=body.bank_account_last4,
            tnc_accepted=body.tnc_accepted,
            notes=body.notes,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class RepointAccountBody(BaseModel):
    linked_account_id: str = Field(..., min_length=4, max_length=64)
    notes: Optional[dict] = None


@router.post("/accounts/{merchant_id}/repoint")
async def repoint_linked_account(
    merchant_id: str = Path(..., min_length=8, max_length=64),
    body: RepointAccountBody = ...,
    _: UserContext = Depends(require_platform_admin()),
):
    """Move an existing linked account (acc_xxx) to a different merchant."""
    try:
        return await route_admin_service.repoint_linked_account(
            merchant_id=merchant_id,
            linked_account_id=body.linked_account_id,
            notes=body.notes,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
