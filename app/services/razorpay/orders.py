"""
Razorpay Orders API service (Phase 2 will fill behaviour).

Phase 1 surface: thin pass-through wrappers around the API endpoints, exposed
so callers can import the symbol today without breaking. Tenant-scoped writes
to `rzp_orders` and the checkout-flow wiring land in Phase 2.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from app.services.razorpay.client import get_razorpay_client


async def create_order(
    *,
    amount_paise: int,
    currency: str = "INR",
    receipt: Optional[str] = None,
    notes: Optional[Mapping[str, Any]] = None,
    partial_payment: bool = False,
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    body: dict[str, Any] = {
        "amount": int(amount_paise),
        "currency": currency,
        "partial_payment": partial_payment,
    }
    if receipt:
        body["receipt"] = receipt
    if notes:
        body["notes"] = dict(notes)
    return await client.post(
        "/v1/orders",
        operation="orders.create",
        json_body=body,
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )


async def fetch_order(order_id: str, *, merchant_id: Optional[str] = None) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/orders/{order_id}",
        operation="orders.fetch",
        merchant_id=merchant_id,
    )


async def list_orders(
    *,
    count: int = 25,
    skip: int = 0,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    expand_payments: bool = False,
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    params: dict[str, Any] = {"count": count, "skip": skip}
    if from_ts is not None:
        params["from"] = from_ts
    if to_ts is not None:
        params["to"] = to_ts
    if expand_payments:
        params["expand[]"] = "payments"
    return await client.get(
        "/v1/orders",
        operation="orders.list",
        params=params,
        merchant_id=merchant_id,
    )


async def fetch_order_payments(
    order_id: str, *, merchant_id: Optional[str] = None
) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/orders/{order_id}/payments",
        operation="orders.payments",
        merchant_id=merchant_id,
    )


async def update_order(
    order_id: str,
    *,
    notes: Mapping[str, Any],
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    return await client.patch(
        f"/v1/orders/{order_id}",
        operation="orders.update",
        json_body={"notes": dict(notes)},
        merchant_id=merchant_id,
    )
