"""
Razorpay Extended APIs — Customers, Plans, Subscriptions, QR Codes.

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

    async def create_plan(
        self,
        plan_name: str,
        amount_paise: int,
        period: str = "monthly",
        interval: int = 1,
        description: str = "",
    ) -> dict:
        """Create a Razorpay subscription plan."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{RAZORPAY_BASE}/plans",
                json={
                    "period": period,
                    "interval": interval,
                    "item": {
                        "name": plan_name,
                        "amount": amount_paise,
                        "currency": "INR",
                        "description": description,
                    },
                },
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        logger.info("razorpay_plan_created", plan_id=data.get("id"))
        return data

    async def get_plan(self, plan_id: str) -> dict:
        """Fetch a Razorpay plan by ID."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{RAZORPAY_BASE}/plans/{plan_id}",
                headers=_headers(),
            )
            resp.raise_for_status()
            return resp.json()

    # ── Subscriptions ──

    async def create_subscription(
        self,
        plan_id: str,
        total_count: int,
        user_id: str = "",
        plan_name: str = "",
        user_email: str = "",
    ) -> dict:
        """Create a Razorpay subscription."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{RAZORPAY_BASE}/subscriptions",
                json={
                    "plan_id": plan_id,
                    "total_count": total_count,
                    "quantity": 1,
                    "customer_notify": 1,
                    "notes": {
                        "user_id": user_id,
                        "plan_name": plan_name,
                        "user_email": user_email,
                    },
                },
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        logger.info("razorpay_subscription_created", sub_id=data.get("id"))
        return data

    async def fetch_subscription(self, subscription_id: str) -> dict:
        """Fetch a Razorpay subscription by ID."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{RAZORPAY_BASE}/subscriptions/{subscription_id}",
                headers=_headers(),
            )
            resp.raise_for_status()
            return resp.json()

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
