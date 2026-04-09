"""Cashfree DigiLocker KYC endpoints."""
import uuid
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel, model_validator
from typing import Optional

from app.core.auth import UserContext, require_permission
from app.core.logging import get_logger
from app.services.digilocker_service import DigiLockerService

router = APIRouter(prefix="/digilocker", tags=["DigiLocker KYC"])
_svc = DigiLockerService()
logger = get_logger(__name__)


class CreateURLIn(BaseModel):
    verification_id: Optional[str] = None
    documents: list[str] = ["AADHAAR", "PAN", "DRIVING_LICENSE"]
    redirect_url: Optional[str] = None
    redirect_to: Optional[str] = None  # Flutter sends this alias
    user_flow: str = "signin"

    @model_validator(mode="after")
    def _fill_defaults(self):
        if not self.verification_id:
            self.verification_id = str(uuid.uuid4())
        if not self.redirect_url:
            self.redirect_url = self.redirect_to or ""
        return self


class VerifyAccountIn(BaseModel):
    verification_id: str
    mobile_number: str


class SaveKycIn(BaseModel):
    verification_id: str
    user_id: Optional[str] = None


@router.post("/verify-account")
async def verify_account(
    body: VerifyAccountIn,
    user: UserContext = Depends(require_permission("kyc.manage")),
):
    """Check if a DigiLocker account exists for the given mobile number."""
    return await _svc.verify_account(body.verification_id, body.mobile_number)


@router.post("/create-url")
async def create_digilocker_url(
    body: CreateURLIn,
    user: UserContext = Depends(require_permission("kyc.manage")),
):
    return await _svc.create_verification_url(
        verification_id=body.verification_id,
        documents=body.documents,
        redirect_url=body.redirect_url,
        user_flow=body.user_flow,
    )


@router.get("/status")
async def get_digilocker_status(
    verification_id: str,
    user: UserContext = Depends(require_permission("kyc.manage")),
):
    return await _svc.get_status(verification_id)


@router.get("/document")
async def get_digilocker_document(
    verification_id: str,
    document_type: str,
    user: UserContext = Depends(require_permission("kyc.manage")),
):
    return await _svc.get_document(verification_id, document_type)


@router.post("/save-kyc")
async def save_kyc(
    body: SaveKycIn,
    user: UserContext = Depends(require_permission("kyc.manage")),
):
    """Check Cashfree status, fetch Aadhaar data, and save KYC to user metadata."""
    uid = body.user_id or user.user_id
    return await _svc.save_kyc(uid, body.verification_id)


@router.post("/webhook")
async def digilocker_webhook(request: Request):
    """Cashfree DigiLocker webhook callback."""
    raw_body = await request.body()
    signature = request.headers.get("x-webhook-signature", "")
    timestamp = request.headers.get("x-webhook-timestamp", "")

    if not _svc.verify_webhook_signature(raw_body, signature, timestamp):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    payload = await request.json()
    event = payload.get("event", "")
    logger.info("digilocker_webhook_received", event=event)

    # Handle verification events
    if event in ("DIGILOCKER_VERIFICATION_SUCCESS", "VERIFICATION_SUCCESS"):
        verification_id = (payload.get("data") or {}).get("verification_id")
        if verification_id:
            # Auto-save KYC when webhook confirms success
            try:
                await _svc.save_kyc_from_webhook(verification_id, payload.get("data", {}))
            except Exception:
                logger.exception("webhook_save_kyc_failed", verification_id=verification_id)

    return {"status": "ok"}
