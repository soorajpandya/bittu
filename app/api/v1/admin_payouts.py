"""
Payout / Disbursement — Admin API (Phase 4).

Prefix:   /admin/payouts
Audience: platform admins (membership in ``platform_admin_users``).
Every endpoint is gated by :func:`require_platform_admin`.

Provides cross-merchant operations:
  • approve / reject / mark sent / completed / failed
  • beneficiary verification + cross-merchant listing
  • batch creation, file generation, batch listing
  • summaries: global, by merchant
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query, Response
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_platform_admin
from app.core.logging import get_logger
from app.services.payout_service import payout_service

router = APIRouter(prefix="/admin/payouts", tags=["Payouts (Admin)"])
logger = get_logger(__name__)


# ── Schemas ──────────────────────────────────────────────────────────────
class AdminBeneficiaryCreate(BaseModel):
    merchant_id:     str
    label:           str = Field(..., min_length=2, max_length=100)
    type:            str = Field(..., pattern=r"^(bank|upi)$")
    account_holder:  Optional[str] = None
    account_number:  Optional[str] = None
    ifsc:            Optional[str] = None
    bank_name:       Optional[str] = None
    upi_vpa:         Optional[str] = None
    metadata:        Optional[dict] = None


class ActionBody(BaseModel):
    notes: Optional[str] = None


class RejectBody(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)
    notes:  Optional[str] = None


class FailBody(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)
    notes:  Optional[str] = None


class MarkSentBody(BaseModel):
    utr_number:     Optional[str] = Field(None, max_length=64)
    bank_reference: Optional[str] = Field(None, max_length=200)
    notes:          Optional[str] = None


class MarkCompletedBody(BaseModel):
    utr_number: Optional[str] = Field(None, max_length=64)
    notes:      Optional[str] = None


class CreateBatchBody(BaseModel):
    merchant_id: Optional[str] = Field(
        None, description="Filter to one merchant; None ⇒ all merchants",
    )
    payout_ids:  Optional[list[str]] = Field(
        None, description="Specific approved payouts; mutually exclusive with merchant_id filter",
    )
    currency:    str = Field("INR", min_length=3, max_length=3)
    notes:       Optional[str] = None


class GenerateFileBody(BaseModel):
    file_format: str = Field("neft_csv", pattern=r"^(neft_csv|imps_csv|upi_csv)$")


# ── Beneficiaries (cross-merchant) ───────────────────────────────────────
@router.get("/beneficiaries")
async def list_beneficiaries(
    merchant_id: Optional[str] = Query(None),
    only_active: bool = Query(True),
    limit: int = Query(100, ge=1, le=500),
    _: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.list_beneficiaries(
        merchant_id=merchant_id, only_active=only_active, limit=limit,
    )


@router.post("/beneficiaries", status_code=201)
async def create_beneficiary(
    body: AdminBeneficiaryCreate,
    user: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.create_beneficiary(
        merchant_id=body.merchant_id, label=body.label, type=body.type,
        account_holder=body.account_holder, account_number=body.account_number,
        ifsc=body.ifsc, bank_name=body.bank_name, upi_vpa=body.upi_vpa,
        metadata=body.metadata, created_by=user.user_id,
    )


@router.post("/beneficiaries/{beneficiary_id}/verify")
async def verify_beneficiary(
    beneficiary_id: str,
    user: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.verify_beneficiary(
        beneficiary_id=beneficiary_id, verified_by=user.user_id,
    )


@router.delete("/beneficiaries/{beneficiary_id}")
async def deactivate_beneficiary(
    beneficiary_id: str,
    _: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.deactivate_beneficiary(beneficiary_id=beneficiary_id)


# ── Payouts: list / get / approve / reject ───────────────────────────────
@router.get("/")
async def list_payouts(
    merchant_id:    Optional[str] = Query(None),
    status:         Optional[str] = Query(None),
    beneficiary_id: Optional[str] = Query(None),
    from_date: Optional[datetime] = Query(None),
    to_date:   Optional[datetime] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    _: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.list_payouts(
        merchant_id=merchant_id, status=status,
        beneficiary_id=beneficiary_id,
        from_date=from_date, to_date=to_date, limit=limit,
    )


@router.get("/summary")
async def summary(
    merchant_id: Optional[str] = Query(None),
    _: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.get_summary(merchant_id=merchant_id)


@router.get("/summary/by-merchant")
async def summary_by_merchant(
    _: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.admin_summary_by_merchant()


@router.get("/{payout_id}")
async def get_payout(
    payout_id: str,
    _: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.get_payout(payout_id=payout_id)


@router.get("/{payout_id}/events")
async def list_events(
    payout_id: str,
    _: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.list_events(payout_id)


@router.post("/{payout_id}/approve")
async def approve_payout(
    payout_id: str,
    body: ActionBody = Body(default_factory=ActionBody),
    user: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.approve_payout(
        payout_id=payout_id, actor_id=user.user_id, notes=body.notes,
    )


@router.post("/{payout_id}/reject")
async def reject_payout(
    payout_id: str,
    body: RejectBody,
    user: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.reject_payout(
        payout_id=payout_id, actor_id=user.user_id,
        reason=body.reason, notes=body.notes,
    )


@router.post("/{payout_id}/mark-sent")
async def mark_sent(
    payout_id: str,
    body: MarkSentBody = Body(default_factory=MarkSentBody),
    user: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.mark_sent(
        payout_id=payout_id, actor_id=user.user_id,
        utr_number=body.utr_number, bank_reference=body.bank_reference,
        notes=body.notes,
    )


@router.post("/{payout_id}/mark-completed")
async def mark_completed(
    payout_id: str,
    body: MarkCompletedBody = Body(default_factory=MarkCompletedBody),
    user: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.mark_completed(
        payout_id=payout_id, actor_id=user.user_id,
        utr_number=body.utr_number, notes=body.notes,
    )


@router.post("/{payout_id}/mark-failed")
async def mark_failed(
    payout_id: str,
    body: FailBody,
    user: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.mark_failed(
        payout_id=payout_id, actor_id=user.user_id,
        reason=body.reason, notes=body.notes,
    )


# ── Batches ──────────────────────────────────────────────────────────────
@router.post("/batches", status_code=201)
async def create_batch(
    body: CreateBatchBody = Body(default_factory=CreateBatchBody),
    user: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.create_batch(
        actor_id=user.user_id, merchant_id=body.merchant_id,
        payout_ids=body.payout_ids, currency=body.currency,
        notes=body.notes,
    )


@router.get("/batches")
async def list_batches(
    status: Optional[str] = Query(None),
    limit:  int = Query(50, ge=1, le=200),
    _: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.list_batches(status=status, limit=limit)


@router.get("/batches/{batch_id}")
async def get_batch(
    batch_id: str,
    _: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.get_batch(batch_id)


@router.get("/batches/{batch_id}/payouts")
async def list_batch_payouts(
    batch_id: str,
    _: UserContext = Depends(require_platform_admin()),
):
    return await payout_service.list_batch_payouts(batch_id)


@router.post("/batches/{batch_id}/generate-file")
async def generate_file(
    batch_id: str,
    body: GenerateFileBody = Body(default_factory=GenerateFileBody),
    download: bool = Query(False, description="If true, returns CSV directly with content-disposition"),
    user: UserContext = Depends(require_platform_admin()),
):
    out = await payout_service.generate_batch_file(
        batch_id=batch_id, actor_id=user.user_id, file_format=body.file_format,
    )
    if download:
        return Response(
            content=out["file_content"],
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{out["file_name"]}"',
            },
        )
    return out
