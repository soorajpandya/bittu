"""
Cashfree Verification / KYC Onboarding — Service.

Handles:
  - 1-Click data availability, OAuth initiation, token exchange, user fetch
  - GST verification
  - Bank reverse penny drop + status check

Auth: x-client-id + x-client-secret + x-cf-signature (RSA-OAEP encrypted)
"""
import base64
import time

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _cfg():
    return get_settings()


def _base_url() -> str:
    return "https://api.cashfree.com/verification"


def _generate_cf_signature(client_id: str, public_key_pem: str) -> str:
    """
    Generate x-cf-signature: RSA-OAEP encrypt '{clientId}.{timestamp}'
    using the Cashfree RSA public key.
    """
    from Crypto.PublicKey import RSA
    from Crypto.Cipher import PKCS1_OAEP

    timestamp = str(int(time.time()))
    message = f"{client_id}.{timestamp}"
    key = RSA.import_key(public_key_pem)
    cipher = PKCS1_OAEP.new(key)
    encrypted = cipher.encrypt(message.encode())
    return base64.b64encode(encrypted).decode()


def _headers() -> dict:
    s = _cfg()
    return {
        "Content-Type": "application/json",
        "x-client-id": s.CF_VERIFY_CLIENT_ID,
        "x-client-secret": s.CF_VERIFY_CLIENT_SECRET,
        "x-cf-signature": _generate_cf_signature(s.CF_VERIFY_CLIENT_ID, s.CF_VERIFY_PUBLIC_KEY),
        "x-api-version": "2024-12-01",
    }


def _oneclick_headers() -> dict:
    """Headers for 1-Click Onboarding endpoints (separate credentials)."""
    s = _cfg()
    return {
        "Content-Type": "application/json",
        "x-client-id": s.CF_ONECLICK_CLIENT_ID,
        "x-client-secret": s.CF_ONECLICK_CLIENT_SECRET,
        "x-cf-signature": _generate_cf_signature(s.CF_ONECLICK_CLIENT_ID, s.CF_VERIFY_PUBLIC_KEY),
        "x-api-version": "2024-12-01",
    }


class CashfreeVerifyService:

    # ── 1-Click Onboarding ──

    async def check_data_availability(self, verification_id: str, phone: str) -> dict:
        """Check if 1-Click data is available for a phone number."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_base_url()}/user/data-availability",
                json={
                    "verification_id": verification_id,
                    "user": [{"identifier_type": "MOBILE", "identifier_value": phone}],
                },
                headers=_headers(),
            )
            resp.raise_for_status()
            return resp.json()

    async def initiate_oauth(self, verification_id: str, phone: str, redirect_url: str) -> dict:
        """Initiate 1-Click OAuth2 session."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_base_url()}/oauth2/session",
                json={
                    "verification_id": verification_id,
                    "redirect_url": redirect_url,
                    "user": {"identifier_type": "MOBILE", "identifier_value": phone},
                },
                headers=_oneclick_headers(),
            )
            resp.raise_for_status()
            return resp.json()

    async def exchange_oauth_token(self, auth_code: str) -> dict:
        """Exchange OAuth auth_code for access_token."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_base_url()}/oauth2/generate-token",
                json={"auth_code": auth_code},
                headers=_oneclick_headers(),
            )
            resp.raise_for_status()
            return resp.json()

    async def fetch_user(self, access_token: str) -> dict:
        """Fetch user details from 1-Click OAuth."""
        hdrs = _oneclick_headers()
        hdrs["Authorization"] = f"Bearer {access_token}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{_base_url()}/oauth2/user-details", headers=hdrs)
            resp.raise_for_status()
            return resp.json()

    # ── GST Verification ──

    async def verify_gst(self, gstin: str, business_name: str = "") -> dict:
        """Verify a GSTIN number."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_base_url()}/gstin",
                json={"GSTIN": gstin, "business_name": business_name},
                headers=_headers(),
            )
            resp.raise_for_status()
            return resp.json()

    # ── Bank Verification ──

    async def bank_reverse_penny_drop(self, verification_id: str, name: str) -> dict:
        """Initiate bank reverse penny drop verification."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_base_url()}/reverse-penny-drop",
                json={"verification_id": verification_id, "name": name},
                headers=_headers(),
            )
            resp.raise_for_status()
            return resp.json()

    async def bank_check_status(self, verification_id: str) -> dict:
        """Check bank verification status."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{_base_url()}/remitter/status",
                params={"verification_id": verification_id},
                headers=_headers(),
            )
            resp.raise_for_status()
            return resp.json()
