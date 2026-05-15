"""
Razorpay Disputes API service (Phase 10 fills sync + alerts).

Phase 1 surface: read-only REST wrappers + evidence submission.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from app.services.razorpay.client import get_razorpay_client


async def fetch_dispute(
    dispute_id: str, *, merchant_id: Optional[str] = None
) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/disputes/{dispute_id}",
        operation="disputes.fetch",
        merchant_id=merchant_id,
    )


async def list_disputes(
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
        "/v1/disputes",
        operation="disputes.list",
        params=params,
        merchant_id=merchant_id,
    )


async def accept_dispute(
    dispute_id: str,
    *,
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    return await client.post(
        f"/v1/disputes/{dispute_id}/accept",
        operation="disputes.accept",
        json_body={},
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )


async def contest_dispute(
    dispute_id: str,
    *,
    evidence: Mapping[str, Any],
    action: str = "draft",                              # draft|submit
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    body = dict(evidence)
    body["action"] = action
    client = await get_razorpay_client()
    return await client.patch(
        f"/v1/disputes/{dispute_id}/contest",
        operation="disputes.contest",
        json_body=body,
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )
