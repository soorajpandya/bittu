"""Cashfree Verification / KYC endpoints."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.cashfree_verify_service import CashfreeVerifyService

router = APIRouter(prefix="/kyc", tags=["KYC / Verification"])
_svc = CashfreeVerifyService()


class DataAvailabilityIn(BaseModel):
    verification_id: str
    phone: str


class InitiateOAuthIn(BaseModel):
    verification_id: str
    phone: str
    redirect_url: str


class ExchangeTokenIn(BaseModel):
    auth_code: str


class FetchUserIn(BaseModel):
    access_token: str


class VerifyGSTIn(BaseModel):
    gstin: str
    business_name: str = ""


class BankVerifyIn(BaseModel):
    verification_id: str
    name: str


class BankStatusIn(BaseModel):
    verification_id: str


@router.post("/data-availability")
async def check_data_availability(
    body: DataAvailabilityIn,
    user: UserContext = Depends(require_permission("kyc.manage")),
):
    return await _svc.check_data_availability(body.verification_id, body.phone)


@router.post("/oauth/initiate")
async def initiate_oauth(
    body: InitiateOAuthIn,
    user: UserContext = Depends(require_permission("kyc.manage")),
):
    return await _svc.initiate_oauth(body.verification_id, body.phone, body.redirect_url)


@router.post("/oauth/token")
async def exchange_token(
    body: ExchangeTokenIn,
    user: UserContext = Depends(require_permission("kyc.manage")),
):
    return await _svc.exchange_oauth_token(body.auth_code)


@router.post("/oauth/user")
async def fetch_user(
    body: FetchUserIn,
    user: UserContext = Depends(require_permission("kyc.manage")),
):
    return await _svc.fetch_user(body.access_token)


@router.post("/verify-gst")
async def verify_gst(
    body: VerifyGSTIn,
    user: UserContext = Depends(require_permission("kyc.manage")),
):
    return await _svc.verify_gst(body.gstin, body.business_name)


@router.post("/bank/reverse-penny-drop")
async def bank_reverse_penny_drop(
    body: BankVerifyIn,
    user: UserContext = Depends(require_permission("kyc.manage")),
):
    return await _svc.bank_reverse_penny_drop(body.verification_id, body.name)


@router.get("/bank/status")
async def bank_check_status(
    verification_id: str,
    user: UserContext = Depends(require_permission("kyc.manage")),
):
    return await _svc.bank_check_status(verification_id)


class DigiLockerVerifyIn(BaseModel):
    verification_id: str | None = None
    code: str | None = None
    status: str | None = None
    message: str | None = None


@router.post("/digilocker-verify")
async def digilocker_verify(
    body: DigiLockerVerifyIn = DigiLockerVerifyIn(),
    user: UserContext = Depends(require_permission("kyc.manage")),
):
    """Process DigiLocker verification callback data from the frontend."""
    from app.services.digilocker_service import DigiLockerService
    dl_svc = DigiLockerService()
    if body.verification_id:
        try:
            result = await dl_svc.get_status(body.verification_id)
            return {"status": "verified", "verification_id": body.verification_id, "data": result}
        except Exception:
            pass
    return {
        "status": "received",
        "verification_id": body.verification_id,
        "message": "KYC verification data received",
    }
