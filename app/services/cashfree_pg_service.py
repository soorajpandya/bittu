"""
Cashfree Payment Gateway — Service.

Flow:
  1. Server calls Cashfree Create Order API
  2. Returns payment_session_id / order_token for client-side drop-in
"""
import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _cfg():
    return get_settings()


def _base_url() -> str:
    return "https://api.cashfree.com/pg"


class CashfreeService:

    async def create_order(
        self,
        order_id: str,
        order_amount: float,
        customer_id: str,
        customer_name: str,
        customer_phone: str,
        return_url: str,
    ) -> dict:
        """Create a Cashfree PG order."""
        s = _cfg()
        payload = {
            "order_id": order_id,
            "order_amount": order_amount,
            "order_currency": "INR",
            "customer_details": {
                "customer_id": customer_id,
                "customer_name": customer_name,
                "customer_phone": customer_phone,
            },
            "order_meta": {"return_url": return_url},
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_base_url()}/orders",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-client-id": s.CASHFREE_APP_ID,
                    "x-client-secret": s.CASHFREE_SECRET_KEY,
                    "x-api-version": "2023-08-01",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        logger.info("cashfree_order_created", order_id=order_id)
        return data
