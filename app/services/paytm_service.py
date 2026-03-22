"""
Paytm Payment Gateway — Service.

Flow:
  1. Server generates HMAC-SHA256 signature of request body
  2. Calls Paytm initiateTransaction API → returns txnToken
  3. Client uses txnToken with Paytm JS Checkout
"""
import hashlib
import hmac
import json

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _cfg():
    return get_settings()


def _base_url() -> str:
    return "https://securegw.paytm.in"


def _generate_signature(body: dict, key: str) -> str:
    """Generate HMAC-SHA256 signature for Paytm request body."""
    payload_str = json.dumps(body, separators=(",", ":"), sort_keys=True)
    return hmac.HMAC(key.encode(), payload_str.encode(), hashlib.sha256).hexdigest()


class PaytmService:

    async def initiate_transaction(
        self,
        order_id: str,
        amount: str,
        cust_id: str,
        callback_url: str,
    ) -> dict:
        """
        Initiate a Paytm transaction.
        Returns txnToken for client-side Paytm JS Checkout.
        """
        s = _cfg()
        body = {
            "requestType": "Payment",
            "mid": s.PAYTM_MID,
            "websiteName": s.PAYTM_WEBSITE,
            "orderId": order_id,
            "txnAmount": {"value": amount, "currency": "INR"},
            "userInfo": {"custId": cust_id},
            "callbackUrl": callback_url,
        }
        signature = _generate_signature(body, s.PAYTM_MERCHANT_KEY)

        url = f"{_base_url()}/theia/api/v1/initiateTransaction?mid={s.PAYTM_MID}&orderId={order_id}"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                json={"body": body, "head": {"signature": signature}},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        txn_token = data.get("body", {}).get("txnToken")
        logger.info("paytm_txn_initiated", order_id=order_id)
        return {
            "txn_token": txn_token,
            "order_id": order_id,
            "mid": s.PAYTM_MID,
            "amount": amount,
        }
