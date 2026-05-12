"""
Merchant KYC — Admin API (Phase 9). Prefix: /admin/merchant-kyc.

Cross-merchant. Requires platform admin. Optional ``merchant_id`` filter on
list endpoints. Document/bank verification + profile review live here.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel

from app.core.auth import UserContext, require_platform_admin
from app.core.exceptions import ValidationError
from app.services.kyc_service import kyc_service

router = APIRouter(prefix="/admin/merchant-kyc", tags=["Merchant KYC (Admin)"])


# ──────────────────────────── pydantic ───────────────────────────────
class _ReviewBody(BaseModel):
    decision: str   # 'approve' | 'reject'
    reason:   Optional[str] = None


class _SuspendBody(BaseModel):
    reason: str


class _DocRejectBody(BaseModel):
    reason: str


class _BankVerifyBody(BaseModel):
    method: str = "manual"
    reference: Optional[str] = None


# ╔═══════════════════════════ profiles ══════════════════════════════╗
@router.get("/profiles")
async def list_profiles(
    status: Optional[str] = Query(None),
    limit:  int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await kyc_service.list_profiles(
        status=status, limit=limit, offset=offset
    )


@router.get("/pending")
async def list_pending(
    limit: int = Query(50, ge=1, le=200),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await kyc_service.list_pending_reviews(limit=limit)


@router.get("/profiles/{merchant_id}")
async def get_profile(
    merchant_id: str = Path(...),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await kyc_service.get_profile(merchant_id)


@router.post("/profiles/{merchant_id}/under-review")
async def set_under_review(
    merchant_id: str = Path(...),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await kyc_service.set_under_review(
        merchant_id, admin_id=admin.user_id
    )


@router.post("/profiles/{merchant_id}/review")
async def review_profile(
    body: _ReviewBody,
    merchant_id: str = Path(...),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await kyc_service.review(
        merchant_id, admin_id=admin.user_id,
        decision=body.decision, reason=body.reason,
    )


@router.post("/profiles/{merchant_id}/suspend")
async def suspend_profile(
    body: _SuspendBody,
    merchant_id: str = Path(...),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await kyc_service.suspend(
        merchant_id, admin_id=admin.user_id, reason=body.reason,
    )


@router.post("/profiles/{merchant_id}/unsuspend")
async def unsuspend_profile(
    merchant_id: str = Path(...),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await kyc_service.unsuspend(
        merchant_id, admin_id=admin.user_id
    )


# ╔═══════════════════════════ documents ═════════════════════════════╗
@router.get("/documents")
async def list_documents(
    merchant_id: Optional[str] = Query(None),
    doc_type:    Optional[str] = Query(None),
    status:      Optional[str] = Query(None),
    limit:  int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await kyc_service.list_documents(
        merchant_id=merchant_id, doc_type=doc_type, status=status,
        limit=limit, offset=offset,
    )


@router.post("/documents/{document_id}/verify")
async def verify_document(
    document_id: int = Path(..., ge=1),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await kyc_service.verify_document(
        document_id, admin_id=admin.user_id,
    )


@router.post("/documents/{document_id}/reject")
async def reject_document(
    body: _DocRejectBody,
    document_id: int = Path(..., ge=1),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await kyc_service.reject_document(
        document_id, admin_id=admin.user_id, reason=body.reason,
    )


# ╔══════════════════════════ bank accounts ══════════════════════════╗
@router.get("/bank-accounts")
async def list_bank_accounts(
    merchant_id: Optional[str] = Query(None),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await kyc_service.list_bank_accounts(merchant_id)


@router.post("/bank-accounts/{bank_id}/verify")
async def verify_bank_account(
    body: _BankVerifyBody,
    bank_id: int = Path(..., ge=1),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await kyc_service.verify_bank_account(
        bank_id, admin_id=admin.user_id,
        method=body.method, reference=body.reference,
    )


# ╔═════════════════════════ audit history ═══════════════════════════╗
@router.get("/audit")
async def list_audit(
    merchant_id: Optional[str] = Query(None),
    limit:  int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin: UserContext = Depends(require_platform_admin()),
):
    return await kyc_service.list_audit_events(
        merchant_id=merchant_id, limit=limit, offset=offset,
    )
