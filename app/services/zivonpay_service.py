"""
Zivonpay Payment Gateway — Service.

Auth: HMAC-SHA256 signature + API Key header
Signature: HMAC-SHA256(api_secret, "txnId|amount|INR|merchant_id")
"""
import hashlib
import hmac

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _cfg():
    return get_settings()


def _base_url() -> str:
    return "https://api.zivonpay.com/v1"


def _generate_signature(txn_id: str, amount: str, merchant_id: str, secret: str) -> str:
    message = f"{txn_id}|{amount}|INR|{merchant_id}"
    return hmac.HMAC(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


class ZivonpayService:

    async def create_order(
        self,
        order_id: str,
        amount: float,
        description: str,
        customer_name: str,
        customer_phone: str,
        return_url: str,
    ) -> dict:
        """Create a Zivonpay payment order."""
        s = _cfg()
        signature = _generate_signature(
            order_id, str(amount), s.ZIVONPAY_MERCHANT_ID, s.ZIVONPAY_API_SECRET
        )

        payload = {
            "order_id": order_id,
            "merchant_id": s.ZIVONPAY_MERCHANT_ID,
            "amount": amount,
            "currency": "INR",
            "description": description,
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "return_url": return_url,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_base_url()}/order",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Api-Key": s.ZIVONPAY_API_KEY,
                    "X-Signature": signature,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        logger.info("zivonpay_order_created", order_id=order_id)
        return data
