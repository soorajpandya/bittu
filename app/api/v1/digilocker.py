"""Cashfree DigiLocker KYC endpoints."""
import uuid
from fastapi import APIRouter, Depends, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
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


@router.get("/callback")
async def digilocker_callback(verification_id: str = Query(...)):
    """
    Redirect endpoint after DigiLocker flow completes.
    Cashfree redirects the user here with ?verification_id=...
    Fetches status and shows a result page / deep-links back to the app.
    """
    try:
        status_data = await _svc.get_status(verification_id)
        status = status_data.get("status", "UNKNOWN")
    except Exception:
        logger.exception("callback_status_check_failed", verification_id=verification_id)
        status = "ERROR"

    # Normalize Cashfree statuses for the Flutter app
    success_statuses = {"VERIFIED", "AUTHENTICATED", "COMPLETED", "SUCCESS"}
    pending_statuses = {"PENDING", "INITIATED", "IN_PROGRESS"}
    if status.upper() in success_statuses:
        app_status = "VERIFIED"
        css_class = "success"
        label = "Verified Successfully ✓"
    elif status.upper() in pending_statuses:
        app_status = "PENDING"
        css_class = "pending"
        label = "Verification Pending..."
    else:
        app_status = "ERROR"
        css_class = "error"
        label = f"Status: {status}"

    # Intent-based deep link for Android (works without registered scheme)
    intent_link = (
        f"intent://kyc/result?verification_id={verification_id}&status={app_status}"
        f"#Intent;scheme=bittu;package=com.bittu.admin;end"
    )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>KYC Verification</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:system-ui,sans-serif;display:flex;justify-content:center;align-items:center;
min-height:100vh;margin:0;background:#f8f9fa;text-align:center}}
.card{{background:#fff;border-radius:12px;padding:2rem;box-shadow:0 2px 12px rgba(0,0,0,.08);max-width:400px}}
.status{{font-size:1.3rem;font-weight:600;margin:1rem 0}}
.success{{color:#16a34a}}.pending{{color:#d97706}}.error{{color:#dc2626}}
.btn{{display:inline-block;margin-top:1rem;padding:.75rem 1.5rem;background:#2563eb;color:#fff;
border-radius:8px;text-decoration:none;font-weight:500;border:none;font-size:1rem;cursor:pointer}}
.hint{{color:#666;font-size:.9rem;margin-top:1rem}}</style></head>
<body><div class="card">
<h2>DigiLocker Verification</h2>
<p class="status {css_class}">{label}</p>
<p>Verification ID: <code>{verification_id[:8]}...</code></p>
<button class="btn" onclick="openApp()">Open Bittu App</button>
<p class="hint">If the app doesn't open, go back to the Bittu app manually.</p>
</div>
<script>
function openApp() {{
  // Try intent link (Android)
  window.location.href = "{intent_link}";
  // Fallback: try custom scheme
  setTimeout(function() {{ window.location.href = "bittu://kyc/result?verification_id={verification_id}&status={app_status}"; }}, 500);
  // Fallback: close browser tab (returns to app that opened it)
  setTimeout(function() {{ window.close(); }}, 1500);
}}
// Auto-trigger on page load
openApp();
</script>
</body></html>"""

    return HTMLResponse(content=html)
