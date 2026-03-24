"""
Cashfree DigiLocker — KYC Document Verification Service.

Handles:
  - Create DigiLocker verification URL (Aadhaar, PAN, Driving License)
  - Check verification status
  - Fetch verified document
  - Save KYC data to user_metadata via Supabase admin API
  - Webhook signature verification + event handling
  - RSA-OAEP x-cf-signature generation (matching Supabase Edge Function)
"""
import base64
import hashlib
import hmac
import time

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Cashfree public key for RSA-OAEP signature generation
_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAsZkKaQ0k6KdUmZ9trMZ1
vC0h5r3jlbv7cF2HGy86DTnkZZ9XGlD5uo4nUVDxVVrGkXU3Qe9Te0dcr3QJEaRm
/Wn3k9s6aHC4yqjNBKhLtb0sygEfQCKCkV6uxcpFLcXXOQNgI3x6IYU/rNMPOzuP
t9F/mdNr1G99qyhshnKNHvFP5Tsy8Od+Af+qAnOvuuNzQfjUHpN7F30VXQhibh60
rbU24OF/wM9/MKfD0hwla7qU4oTgGd8IfWFJ/fqT17u9H+5yOo+80TkyNKzXK5zY
sKKSXG0LjEHdr7ut+ee/pRwIZ/kk3JYU0gVr+EwkAvi8eAEsAHzHLJzho1PRWTM/
jQIDAQAB
-----END PUBLIC KEY-----"""

_rsa_key = None


def _get_rsa_key():
    """Import RSA public key for x-cf-signature generation."""
    global _rsa_key
    if _rsa_key is not None:
        return _rsa_key
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    _rsa_key = load_pem_public_key(_PUBLIC_KEY_PEM.encode())
    return _rsa_key


def _generate_signature(client_id: str) -> str:
    """Generate x-cf-signature: Base64(RSA-OAEP-encrypt(clientId.unixTimestamp, publicKey))."""
    from cryptography.hazmat.primitives.asymmetric.padding import OAEP, MGF1
    from cryptography.hazmat.primitives.hashes import SHA1
    timestamp = int(time.time())
    plaintext = f"{client_id}.{timestamp}"
    key = _get_rsa_key()
    encrypted = key.encrypt(
        plaintext.encode(),
        OAEP(mgf=MGF1(algorithm=SHA1()), algorithm=SHA1(), label=None),
    )
    return base64.b64encode(encrypted).decode()


def _cfg():
    return get_settings()


def _base_url() -> str:
    mode = _cfg().CF_DIGILOCKER_MODE
    host = "https://api.cashfree.com" if mode in ("production", "live") else "https://sandbox.cashfree.com"
    return f"{host}/verification"


def _headers() -> dict:
    s = _cfg()
    h = {
        "Content-Type": "application/json",
        "x-client-id": s.CF_DIGILOCKER_CLIENT_ID,
        "x-client-secret": s.CF_DIGILOCKER_CLIENT_SECRET,
    }
    try:
        h["x-cf-signature"] = _generate_signature(s.CF_DIGILOCKER_CLIENT_ID)
    except Exception:
        logger.warning("cf_signature_generation_failed")
    return h


class DigiLockerService:

    async def verify_account(self, verification_id: str, mobile_number: str) -> dict:
        """Check if a DigiLocker account exists for the given mobile number."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_base_url()}/digilocker/verify-account",
                json={
                    "verification_id": verification_id,
                    "mobile_number": mobile_number,
                },
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()

        logger.info("digilocker_verify_account", verification_id=verification_id, status=data.get("status"))
        return data

    async def create_verification_url(
        self,
        verification_id: str,
        documents: list[str],
        redirect_url: str,
        user_flow: str = "REDIRECT",
    ) -> dict:
        """
        Create a DigiLocker verification URL.
        documents: list of ["AADHAAR", "PAN", "DRIVING_LICENSE"]
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_base_url()}/digilocker",
                json={
                    "verification_id": verification_id,
                    "document_requested": documents,
                    "redirect_url": redirect_url,
                    "user_flow": user_flow,
                },
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()

        logger.info("digilocker_url_created", verification_id=verification_id)
        return data

    async def get_status(self, verification_id: str) -> dict:
        """Get DigiLocker verification status."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{_base_url()}/digilocker",
                    params={"verification_id": verification_id},
                    headers=_headers(),
                )
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning("digilocker_status_upstream_error", verification_id=verification_id, status=e.response.status_code)
            return {"verification_id": verification_id, "status": "pending", "message": "Verification not yet processed"}
        except httpx.RequestError:
            logger.warning("digilocker_status_request_failed", verification_id=verification_id)
            return {"verification_id": verification_id, "status": "pending", "message": "Unable to reach verification service"}

    async def get_document(self, verification_id: str, document_type: str) -> dict:
        """Fetch a verified document. document_type: AADHAAR | PAN | DRIVING_LICENSE"""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{_base_url()}/digilocker/document/{document_type}",
                params={"verification_id": verification_id},
                headers=_headers(),
            )
            resp.raise_for_status()
            return resp.json()

    def verify_webhook_signature(self, raw_body: bytes, signature: str, timestamp: str) -> bool:
        """Verify Cashfree DigiLocker webhook signature (HMAC-SHA256)."""
        s = _cfg()
        message = f"{timestamp}{raw_body.decode()}"
        expected = base64.b64encode(
            hmac.HMAC(
                s.CF_DIGILOCKER_CLIENT_SECRET.encode(),
                message.encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        return hmac.compare_digest(expected, signature)

    async def save_kyc(self, user_id: str, verification_id: str) -> dict:
        """
        Verify with Cashfree that verification is complete, fetch Aadhaar doc,
        and save KYC data to Supabase user_metadata via admin API.
        Mirrors the Supabase Edge Function save_kyc action.
        """
        s = _cfg()

        # 1. Check verification status with Cashfree
        status_data = await self.get_status(verification_id)
        cf_status = status_data.get("status", "pending")

        # Build KYC payload even if not AUTHENTICATED (store whatever we have)
        kyc_data = {
            "verification_id": verification_id,
            "status": cf_status,
            "user_details": status_data.get("user_details"),
            "document_consent": status_data.get("document_consent"),
        }

        # 2. If authenticated, fetch Aadhaar document
        if cf_status == "AUTHENTICATED":
            try:
                aadhaar = await self.get_document(verification_id, "AADHAAR")
                if aadhaar and not aadhaar.get("error"):
                    kyc_data["aadhaar"] = aadhaar
            except Exception:
                logger.warning("save_kyc_aadhaar_fetch_failed", verification_id=verification_id)

        # 3. Update user_metadata via Supabase admin API
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.put(
                f"{s.SUPABASE_URL}/auth/v1/admin/users/{user_id}",
                json={"user_metadata": {"digilocker_verification": kyc_data}},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {s.SUPABASE_SERVICE_ROLE_KEY}",
                    "apikey": s.SUPABASE_SERVICE_ROLE_KEY,
                },
            )
            if not resp.is_success:
                logger.error("save_kyc_admin_update_failed", status=resp.status_code, user_id=user_id)
                return {"success": False, "error": "Failed to save KYC data"}

        logger.info("save_kyc_success", user_id=user_id, verification_id=verification_id, status=cf_status)
        return {"success": True, "kyc_data": kyc_data}

    async def save_kyc_from_webhook(self, verification_id: str, webhook_data: dict) -> None:
        """
        Called from webhook handler when verification succeeds.
        Looks up user_id from kyc_verifications table, then saves KYC.
        """
        from app.core.database import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id FROM kyc_verifications WHERE verification_id = $1",
                verification_id,
            )

        if not row:
            logger.warning("webhook_no_user_for_verification", verification_id=verification_id)
            return

        user_id = str(row["user_id"])
        await self.save_kyc(user_id, verification_id)
