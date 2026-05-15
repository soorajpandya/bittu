"""
Razorpay Refunds API service (Phase 9 fills orchestrator + ledger reversal).

Phase 1 surface: REST wrappers around `/v1/payments/{id}/refund` &
`/v1/refunds`.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from app.services.razorpay.client import get_razorpay_client


async def create_refund(
    *,
    payment_id: str,
    amount_paise: Optional[int] = None,
    speed: str = "normal",                 # normal|optimum
    notes: Optional[Mapping[str, Any]] = None,
    receipt: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    body: dict[str, Any] = {"speed": speed}
    if amount_paise is not None:
        body["amount"] = int(amount_paise)
    if notes:
        body["notes"] = dict(notes)
    if receipt:
        body["receipt"] = receipt

    client = await get_razorpay_client()
    return await client.post(
        f"/v1/payments/{payment_id}/refund",
        operation="refunds.create",
        json_body=body,
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )


async def fetch_refund(refund_id: str, *, merchant_id: Optional[str] = None) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/refunds/{refund_id}",
        operation="refunds.fetch",
        merchant_id=merchant_id,
    )


async def list_refunds(
    *,
    count: int = 25,
    skip: int = 0,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    payment_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    params: dict[str, Any] = {"count": count, "skip": skip}
    if from_ts is not None:
        params["from"] = from_ts
    if to_ts is not None:
        params["to"] = to_ts
    path = (
        f"/v1/payments/{payment_id}/refunds" if payment_id else "/v1/refunds"
    )
    op = "refunds.list_for_payment" if payment_id else "refunds.list"
    return await client.get(path, operation=op, params=params, merchant_id=merchant_id)


async def update_refund_notes(
    refund_id: str,
    *,
    notes: Mapping[str, Any],
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    return await client.patch(
        f"/v1/refunds/{refund_id}",
        operation="refunds.update",
        json_body={"notes": dict(notes)},
        merchant_id=merchant_id,
    )
