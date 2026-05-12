"""
Merchant KYC — Merchant API (Phase 9). Prefix: /merchant-kyc.

Distinct from the existing /kyc (Cashfree user-level verification).
Scoped to the caller's merchant. No gateway wiring.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.exceptions import ValidationError
from app.services.kyc_service import kyc_service

router = APIRouter(prefix="/merchant-kyc", tags=["KYC"])


def _mid(user: UserContext) -> str:
    if not user.restaurant_id:
        raise ValidationError("Active restaurant context is required.")
    return str(user.restaurant_id)


# ──────────────────────────── pydantic models ────────────────────────
class _ProfileUpdate(BaseModel):
    legal_name:         Optional[str] = None
    business_type:      Optional[str] = None
    pan:                Optional[str] = None
    gstin:              Optional[str] = None
    cin:                Optional[str] = None
    registered_address: Optional[dict] = None
    contact_email:      Optional[str] = None
    contact_phone:      Optional[str] = None
    website:            Optional[str] = None
    metadata:           Optional[dict] = None


class _DocCreate(BaseModel):
    doc_type: str
    file_url: str
    mime_type:  Optional[str] = None
    size_bytes: Optional[int] = None
    file_hash:  Optional[str] = None
    owner_id:   Optional[int] = None
    expires_at: Optional[str] = None
    metadata:   Optional[dict] = None


class _OwnerCreate(BaseModel):
    full_name:     str
    role:          str
    dob:           Optional[str] = None
    pan:           Optional[str] = None
    aadhaar_last4: Optional[str] = None
    ownership_pct: float = 0
    email:         Optional[str] = None
    phone:         Optional[str] = None
    is_signatory:  bool = False
    metadata:      Optional[dict] = None


class _BankCreate(BaseModel):
    account_holder_name: str
    account_number:      str
    ifsc:                str
    bank_name:           Optional[str] = None
    branch:              Optional[str] = None
    account_type:        str = "current"
    is_primary:          bool = False
    metadata:            Optional[dict] = None


# ╔════════════════════════════ profile ══════════════════════════════╗
@router.get("/profile")
async def get_profile(
    user: UserContext = Depends(require_permission("kyc.read")),
):
    return await kyc_service.get_or_create_profile(_mid(user))


@router.put("/profile")
async def update_profile(
    body: _ProfileUpdate,
    user: UserContext = Depends(require_permission("kyc.write")),
):
    return await kyc_service.update_profile(
        _mid(user), **body.model_dump(exclude_none=True)
    )


@router.post("/submit")
async def submit_profile(
    user: UserContext = Depends(require_permission("kyc.write")),
):
    return await kyc_service.submit(_mid(user), actor_user_id=user.user_id)


# ╔════════════════════════════ documents ════════════════════════════╗
@router.get("/documents")
async def list_documents(
    doc_type: Optional[str] = Query(None),
    status:   Optional[str] = Query(None),
    limit:    int = Query(100, ge=1, le=500),
    offset:   int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("kyc.read")),
):
    return await kyc_service.list_documents(
        merchant_id=_mid(user), doc_type=doc_type, status=status,
        limit=limit, offset=offset,
    )


@router.post("/documents")
async def add_document(
    body: _DocCreate,
    user: UserContext = Depends(require_permission("kyc.write")),
):
    return await kyc_service.add_document(
        _mid(user),
        uploaded_by_user_id=user.user_id,
        **body.model_dump(exclude_none=True),
    )


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: int = Path(..., ge=1),
    user: UserContext = Depends(require_permission("kyc.write")),
):
    await kyc_service.delete_document(
        document_id, merchant_id=_mid(user), actor_user_id=user.user_id,
    )
    return {"deleted": True, "document_id": document_id}


# ╔════════════════════════════ owners ═══════════════════════════════╗
@router.get("/owners")
async def list_owners(
    user: UserContext = Depends(require_permission("kyc.read")),
):
    return await kyc_service.list_owners(_mid(user))


@router.post("/owners")
async def add_owner(
    body: _OwnerCreate,
    user: UserContext = Depends(require_permission("kyc.write")),
):
    return await kyc_service.add_owner(
        _mid(user), **body.model_dump(exclude_none=True)
    )


@router.delete("/owners/{owner_id}")
async def remove_owner(
    owner_id: int = Path(..., ge=1),
    user: UserContext = Depends(require_permission("kyc.write")),
):
    await kyc_service.remove_owner(
        owner_id, merchant_id=_mid(user), actor_user_id=user.user_id,
    )
    return {"deleted": True, "owner_id": owner_id}


# ╔══════════════════════════ bank accounts ══════════════════════════╗
@router.get("/bank-accounts")
async def list_bank_accounts(
    user: UserContext = Depends(require_permission("kyc.read")),
):
    return await kyc_service.list_bank_accounts(_mid(user))


@router.post("/bank-accounts")
async def add_bank_account(
    body: _BankCreate,
    user: UserContext = Depends(require_permission("kyc.write")),
):
    return await kyc_service.add_bank_account(
        _mid(user), **body.model_dump(exclude_none=True)
    )


@router.post("/bank-accounts/{bank_id}/primary")
async def make_primary(
    bank_id: int = Path(..., ge=1),
    user: UserContext = Depends(require_permission("kyc.write")),
):
    return await kyc_service.set_primary_bank(
        bank_id, merchant_id=_mid(user)
    )


# ╔═════════════════════════ audit history ═══════════════════════════╗
@router.get("/audit")
async def list_audit(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("kyc.read")),
):
    return await kyc_service.list_audit_events(
        merchant_id=_mid(user), limit=limit, offset=offset,
    )
