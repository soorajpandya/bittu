"""
Razorpay Payments API service (Phase 5 fills storage + ledger wiring).

Phase 1 surface: pure pass-through wrappers. Signature verification helper
is included now since orders.py and webhooks.py both depend on it.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any, Mapping, Optional

from app.core.config import get_settings
from app.services.razorpay.client import get_razorpay_client


# ── signature helpers ─────────────────────────────────────────────────────


def verify_order_payment_signature(
    *, razorpay_order_id: str, razorpay_payment_id: str, signature: str
) -> bool:
    """Client-side checkout-form signature verification."""
    secret = get_settings().RAZORPAY_KEY_SECRET.encode()
    msg = f"{razorpay_order_id}|{razorpay_payment_id}".encode()
    expected = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_webhook_signature(*, body: bytes, signature: str) -> bool:
    """Webhook signature verification using the dedicated webhook secret."""
    secret = get_settings().RAZORPAY_WEBHOOK_SECRET.encode()
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── REST wrappers ─────────────────────────────────────────────────────────


async def fetch_payment(payment_id: str, *, merchant_id: Optional[str] = None) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/payments/{payment_id}",
        operation="payments.fetch",
        merchant_id=merchant_id,
    )


async def list_payments(
    *,
    count: int = 25,
    skip: int = 0,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    params: dict[str, Any] = {"count": count, "skip": skip}
    if from_ts is not None:
        params["from"] = from_ts
    if to_ts is not None:
        params["to"] = to_ts
    return await client.get(
        "/v1/payments",
        operation="payments.list",
        params=params,
        merchant_id=merchant_id,
    )


async def capture_payment(
    payment_id: str,
    *,
    amount_paise: int,
    currency: str = "INR",
    merchant_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    return await client.post(
        f"/v1/payments/{payment_id}/capture",
        operation="payments.capture",
        json_body={"amount": int(amount_paise), "currency": currency},
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )


async def update_payment_notes(
    payment_id: str,
    *,
    notes: Mapping[str, Any],
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    return await client.patch(
        f"/v1/payments/{payment_id}",
        operation="payments.update",
        json_body={"notes": dict(notes)},
        merchant_id=merchant_id,
    )
