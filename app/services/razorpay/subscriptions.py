"""
Razorpay Subscriptions API service.

Thin async wrappers around the Razorpay Subscriptions REST endpoints, going
through the shared :func:`get_razorpay_client` chokepoint (auth, retries,
idempotency, audit into ``rzp_api_calls``).

Used by the onboarding SaaS-subscription flow: the backend creates a
per-merchant subscription against a dashboard-defined Plan id, the FE collects
the mandate via Razorpay Checkout, and the verify endpoint + ``subscription.*``
webhooks keep ``merchant_subscriptions`` in sync.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from app.services.razorpay.client import get_razorpay_client


async def create_subscription(
    *,
    plan_id: str,
    total_count: int,
    customer_notify: bool = True,
    quantity: int = 1,
    notes: Optional[Mapping[str, Any]] = None,
    expire_by: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    """Create a subscription for ``plan_id``.

    Returns the Razorpay subscription entity (includes ``id`` and
    ``short_url`` — the hosted authorization link).
    """
    client = await get_razorpay_client()
    body: dict[str, Any] = {
        "plan_id": plan_id,
        "total_count": int(total_count),
        "customer_notify": 1 if customer_notify else 0,
        "quantity": int(quantity),
    }
    if expire_by is not None:
        body["expire_by"] = int(expire_by)
    if notes:
        body["notes"] = dict(notes)
    return await client.post(
        "/v1/subscriptions",
        operation="subscriptions.create",
        json_body=body,
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )


async def fetch_subscription(
    subscription_id: str, *, merchant_id: Optional[str] = None
) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/subscriptions/{subscription_id}",
        operation="subscriptions.fetch",
        merchant_id=merchant_id,
    )


async def cancel_subscription(
    subscription_id: str,
    *,
    cancel_at_cycle_end: bool = False,
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    return await client.post(
        f"/v1/subscriptions/{subscription_id}/cancel",
        operation="subscriptions.cancel",
        json_body={"cancel_at_cycle_end": 1 if cancel_at_cycle_end else 0},
        merchant_id=merchant_id,
    )
