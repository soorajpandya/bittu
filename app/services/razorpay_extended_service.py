"""
Razorpay Extended APIs — Customers, QR Codes.

Supplements the core PaymentService (which handles orders, verification, webhooks).
All calls use Basic Auth: base64(key_id:key_secret).
"""
import httpx
import base64

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

RAZORPAY_BASE = "https://api.razorpay.com/v1"


def _cfg():
    return get_settings()


def _auth_header() -> str:
    s = _cfg()
    creds = base64.b64encode(f"{s.RAZORPAY_KEY_ID}:{s.RAZORPAY_KEY_SECRET}".encode()).decode()
    return f"Basic {creds}"


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": _auth_header(),
    }


class RazorpayExtendedService:

    # ── Customers ──

    async def create_customer(
        self, name: str, email: str, contact: str, fail_existing: str = "0"
    ) -> dict:
        """Create a Razorpay customer."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{RAZORPAY_BASE}/customers",
                json={"name": name, "email": email, "contact": contact, "fail_existing": fail_existing},
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        logger.info("razorpay_customer_created", customer_id=data.get("id"))
        return data

    # ── Plans ──

    # ── QR Codes ──

    async def create_qr_code(
        self,
        name: str,
        amount_paise: int,
        description: str = "",
        close_by: int | None = None,
    ) -> dict:
        """Create a UPI QR code for single-use payment."""
        payload: dict = {
            "type": "upi_qr",
            "name": name,
            "usage": "single_use",
            "fixed_amount": True,
            "payment_amount": amount_paise,
            "description": description,
        }
        if close_by:
            payload["close_by"] = close_by

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{RAZORPAY_BASE}/payments/qr_codes",
                json=payload,
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        logger.info("razorpay_qr_created", qr_id=data.get("id"))
        return data
