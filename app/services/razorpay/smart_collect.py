"""
Razorpay Smart Collect — virtual accounts (Phase 11 fills recon).

Phase 1 surface: REST wrappers for /v1/virtual_accounts.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from app.services.razorpay.client import get_razorpay_client


async def create_virtual_account(
    *,
    receivers_types: Sequence[str],                 # ["bank_account"], ["vpa"], ["bank_account","vpa"]
    descriptor: Optional[str] = None,               # custom UPI handle suffix
    customer_id: Optional[str] = None,
    description: Optional[str] = None,
    amount_expected_paise: Optional[int] = None,
    notes: Optional[Mapping[str, Any]] = None,
    allowed_payers: Optional[Sequence[Mapping[str, Any]]] = None,
    close_by: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    receivers: dict[str, Any] = {"types": list(receivers_types)}
    if descriptor and "vpa" in receivers_types:
        receivers["vpa"] = {"descriptor": descriptor}
    body: dict[str, Any] = {"receivers": receivers}
    if customer_id:
        body["customer_id"] = customer_id
    if description:
        body["description"] = description
    if amount_expected_paise is not None:
        body["amount_expected"] = int(amount_expected_paise)
    if notes:
        body["notes"] = dict(notes)
    if allowed_payers:
        body["allowed_payers"] = [dict(p) for p in allowed_payers]
    if close_by:
        body["close_by"] = int(close_by)

    client = await get_razorpay_client()
    return await client.post(
        "/v1/virtual_accounts",
        operation="smart_collect.va.create",
        json_body=body,
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )


async def fetch_virtual_account(
    va_id: str, *, merchant_id: Optional[str] = None
) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/virtual_accounts/{va_id}",
        operation="smart_collect.va.fetch",
        merchant_id=merchant_id,
    )


async def list_virtual_accounts(
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
        "/v1/virtual_accounts",
        operation="smart_collect.va.list",
        params=params,
        merchant_id=merchant_id,
    )


async def fetch_va_payments(
    va_id: str,
    *,
    count: int = 25,
    skip: int = 0,
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/virtual_accounts/{va_id}/payments",
        operation="smart_collect.va.payments",
        params={"count": count, "skip": skip},
        merchant_id=merchant_id,
    )


async def close_virtual_account(
    va_id: str, *, merchant_id: Optional[str] = None
) -> dict:
    client = await get_razorpay_client()
    return await client.post(
        f"/v1/virtual_accounts/{va_id}/close",
        operation="smart_collect.va.close",
        json_body={},
        merchant_id=merchant_id,
    )


async def add_allowed_payer(
    va_id: str,
    *,
    payer: Mapping[str, Any],
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    return await client.post(
        f"/v1/virtual_accounts/{va_id}/allowed_payers",
        operation="smart_collect.va.allowed_payer.add",
        json_body=dict(payer),
        merchant_id=merchant_id,
    )


async def fetch_va_payment_details(
    va_id: str, payment_id: str, *, merchant_id: Optional[str] = None
) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/payments/{payment_id}/bank_transfer",
        operation="smart_collect.payment.bank_transfer",
        merchant_id=merchant_id,
    )
