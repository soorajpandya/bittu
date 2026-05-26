"""
``/api/v1/bankkyc_razorpay`` — batch-upload-driven Razorpay Linked-Account
onboarding.

Replaces direct calls to the Razorpay Route ``POST /v2/accounts`` flow.
Razorpay only supports bulk linked-account creation via manual CSV upload
on their Dashboard, so we:

1. Persist the merchant submission in Supabase.
2. Group submissions into 30-minute batches.
3. Expose CSV / XLSX downloads to admins, who upload them manually.
4. Track status transitions back to the merchant.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.core.auth import UserContext, require_permission, require_platform_admin
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.services.razorpay.kyc_batch_service import rzp_kyc_batch_service

logger = get_logger(__name__)


router = APIRouter(prefix="/bankkyc_razorpay", tags=["Razorpay KYC Batch"])


# ── helpers ────────────────────────────────────────────────────────────────
def _mid(user: UserContext) -> str:
    if not user.restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    return str(user.restaurant_id)


# ── DTOs ───────────────────────────────────────────────────────────────────
class BankKycSubmitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_name:     str = Field(..., min_length=2, max_length=200)
    account_email:    str = Field(..., min_length=4, max_length=200)
    business_name:    str = Field(..., min_length=2, max_length=200)
    business_type:    str = Field(..., min_length=2, max_length=40)
    ifsc_code:        str = Field(..., min_length=11, max_length=11)
    account_number:   str = Field(..., min_length=4, max_length=35)
    beneficiary_name: str = Field(..., min_length=2, max_length=200)
    dashboard_access: int = Field(0, ge=0, le=1)
    customer_refunds: int = Field(0, ge=0, le=1)
    notes:            Optional[dict[str, Any]] = None


class MarkApprovedIn(BaseModel):
    razorpay_account_ids: Optional[dict[int, str]] = Field(
        None,
        description="Optional {submission_id: acc_xxx} map captured "
                    "from the Razorpay Dashboard after a successful upload.",
    )


class MarkRejectedIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=500)


class MarkSubmissionApprovedIn(BaseModel):
    razorpay_account_id: Optional[str] = Field(None, min_length=4, max_length=64)


# ── merchant-facing endpoints ──────────────────────────────────────────────
@router.post("", status_code=200, summary="Submit bank/KYC details for batch upload")
async def submit_bank_kyc(
    payload: BankKycSubmitIn = Body(...),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    """Persist the merchant's KYC payload. No Razorpay APIs are called —
    the actual linked-account creation happens later via a manually-
    uploaded CSV on the Razorpay Dashboard."""
    merchant_id = _mid(user)
    try:
        row = await rzp_kyc_batch_service.submit(
            merchant_id=merchant_id,
            **payload.model_dump(exclude_none=False),
        )
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    eta = rzp_kyc_batch_service.eta_payload()
    return {
        "success": True,
        "message": rzp_kyc_batch_service.SUBMISSION_OK_MESSAGE,
        "submission_id": row["id"],
        "status":  row["status"],
        **eta,
    }


@router.get("/status", summary="Merchant's own KYC status + ETA")
async def my_kyc_status(
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    return await rzp_kyc_batch_service.get_merchant_status(_mid(user))


# ── admin endpoints ────────────────────────────────────────────────────────
@router.get("/admin/stats", summary="Batch + submission metrics for dashboard")
async def admin_stats(
    _: UserContext = Depends(require_platform_admin()),
):
    return await rzp_kyc_batch_service.stats()


@router.get("/admin/batches", summary="List recent batches")
async def admin_list_batches(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: UserContext = Depends(require_platform_admin()),
):
    return {"items": await rzp_kyc_batch_service.list_batches(limit=limit, offset=offset)}


@router.get("/admin/submissions", summary="List submissions (optional status filter)")
async def admin_list_submissions(
    status: Optional[str] = Query(None),
    batch_id: Optional[int] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _: UserContext = Depends(require_platform_admin()),
):
    return {
        "items": await rzp_kyc_batch_service.list_submissions(
            status=status, batch_id=batch_id, limit=limit, offset=offset,
        ),
    }


@router.post("/admin/batches/generate", summary="Force-generate the current 30-min batch")
async def admin_generate_batch(
    _: UserContext = Depends(require_platform_admin()),
):
    return await rzp_kyc_batch_service.generate_batch_for_slot()


@router.get("/admin/batches/{batch_id}/csv", summary="Download batch as CSV")
async def admin_download_csv(
    batch_id: int,
    _: UserContext = Depends(require_platform_admin()),
):
    try:
        name, blob = await rzp_kyc_batch_service.get_batch_csv(batch_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return Response(
        content=blob,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/admin/batches/{batch_id}/xlsx", summary="Download batch as Razorpay-template XLSX")
async def admin_download_xlsx(
    batch_id: int,
    _: UserContext = Depends(require_platform_admin()),
):
    try:
        name, blob = await rzp_kyc_batch_service.get_batch_xlsx(batch_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return Response(
        content=blob,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/admin/batches/{batch_id}/mark-uploaded", summary="Mark batch uploaded to Razorpay")
async def admin_mark_uploaded(
    batch_id: int,
    user: UserContext = Depends(require_platform_admin()),
):
    try:
        return await rzp_kyc_batch_service.mark_uploaded(batch_id, actor_id=user.user_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/admin/batches/{batch_id}/mark-approved", summary="Mark all batch submissions APPROVED")
async def admin_mark_approved(
    batch_id: int,
    payload: MarkApprovedIn = Body(default_factory=MarkApprovedIn),
    user: UserContext = Depends(require_platform_admin()),
):
    try:
        return await rzp_kyc_batch_service.mark_batch_approved(
            batch_id,
            actor_id=user.user_id,
            razorpay_account_ids=payload.razorpay_account_ids,
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/admin/batches/{batch_id}/mark-rejected", summary="Mark batch (and remaining submissions) REJECTED")
async def admin_mark_rejected(
    batch_id: int,
    payload: MarkRejectedIn = Body(default_factory=MarkRejectedIn),
    user: UserContext = Depends(require_platform_admin()),
):
    try:
        return await rzp_kyc_batch_service.mark_batch_rejected(
            batch_id, reason=payload.reason, actor_id=user.user_id,
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/admin/submissions/{submission_id}/mark-approved")
async def admin_mark_submission_approved(
    submission_id: int,
    payload: MarkSubmissionApprovedIn = Body(default_factory=MarkSubmissionApprovedIn),
    _: UserContext = Depends(require_platform_admin()),
):
    try:
        return await rzp_kyc_batch_service.mark_submission_approved(
            submission_id, razorpay_account_id=payload.razorpay_account_id,
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/admin/submissions/{submission_id}/mark-rejected")
async def admin_mark_submission_rejected(
    submission_id: int,
    payload: MarkRejectedIn = Body(default_factory=MarkRejectedIn),
    _: UserContext = Depends(require_platform_admin()),
):
    try:
        return await rzp_kyc_batch_service.mark_submission_rejected(
            submission_id, reason=payload.reason,
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post(
    "/admin/submissions/{submission_id}/check-account",
    summary="Reconcile against Razorpay GET /v2/accounts/{id}",
)
async def admin_check_account(
    submission_id: int,
    _: UserContext = Depends(require_platform_admin()),
):
    try:
        return await rzp_kyc_batch_service.check_account_status(submission_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
