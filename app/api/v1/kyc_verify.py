"""Merchant-facing KYC verification endpoints (Attestr-backed)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.services.attestr_service import (
    FssaiVerificationResult,
    verify_fssai_license,
)

router = APIRouter(prefix="/kyc/verify", tags=["KYC"])


class _FssaiVerifyIn(BaseModel):
    reg: str = Field(..., min_length=14, max_length=14, pattern=r"^\d{14}$")
    fetch_products: bool = False


@router.post("/fssai", response_model=None)
async def verify_fssai(
    body: _FssaiVerifyIn,
    user: UserContext = Depends(require_permission("kyc:verify")),
) -> FssaiVerificationResult:
    """
    Verify an FSSAI license via Attestr.

    Returns the service's normalized payload as-is — see
    `FssaiVerificationResult` for shape. `valid=false` is a successful 200,
    not an error; inspect `valid` + `message`.
    """
    return await verify_fssai_license(body.reg, fetch_products=body.fetch_products)
