"""
Razorpay Settlements API service (Phase 7 fills sync + recon).

Phase 1 surface: REST wrappers for `/v1/settlements` and the recon-statement
report endpoint that powers EOD reconciliation.
"""
from __future__ import annotations

from typing import Any, Optional

from app.services.razorpay.client import get_razorpay_client


async def fetch_settlement(
    settlement_id: str, *, merchant_id: Optional[str] = None
) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/settlements/{settlement_id}",
        operation="settlements.fetch",
        merchant_id=merchant_id,
    )


async def list_settlements(
    *,
    count: int = 25,
    skip: int = 0,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    merchant_id: Optional[str] = None,
    account_id: Optional[str] = None,
) -> dict:
    """List settlements.

    ``merchant_id`` is recorded on the API-call audit row. ``account_id``
    is the Razorpay linked-account id (``acc_xxx``) and, when supplied,
    causes the underlying client to send ``X-Razorpay-Account`` so the
    response is scoped to *that* linked account's settlements instead of
    the platform's master account.
    """
    client = await get_razorpay_client()
    params: dict[str, Any] = {"count": count, "skip": skip}
    if from_ts is not None:
        params["from"] = from_ts
    if to_ts is not None:
        params["to"] = to_ts
    return await client.get(
        "/v1/settlements",
        operation="settlements.list",
        params=params,
        merchant_id=merchant_id,
        account_id=account_id,
    )


async def list_settlement_recon(
    *,
    year: int,
    month: int,
    day: Optional[int] = None,
    count: int = 100,
    skip: int = 0,
    merchant_id: Optional[str] = None,
    account_id: Optional[str] = None,
) -> dict:
    """Settlement recon report — `combined`, used for daily reconciliation.

    Pass ``account_id`` to scope the report to a specific linked account
    via the ``X-Razorpay-Account`` header.
    """
    client = await get_razorpay_client()
    params: dict[str, Any] = {
        "year": year, "month": month,
        "count": count, "skip": skip,
    }
    if day is not None:
        params["day"] = day
    return await client.get(
        "/v1/settlements/recon/combined",
        operation="settlements.recon",
        params=params,
        merchant_id=merchant_id,
        account_id=account_id,
    )
