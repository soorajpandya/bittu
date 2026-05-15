"""
Razorpay Route APIs — linked accounts + transfers (Phase 6 wiring).

Phase 1 surface: REST wrappers for /v2/accounts, /v1/transfers,
/v1/payments/{id}/transfers.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from app.services.razorpay.client import get_razorpay_client


# ── Linked accounts (v2) ─────────────────────────────────────────────────


async def create_linked_account(
    *,
    email: str,
    phone: str,
    legal_business_name: str,
    business_type: str,
    contact_name: str,
    profile: Mapping[str, Any],
    legal_info: Optional[Mapping[str, Any]] = None,
    brand: Optional[Mapping[str, Any]] = None,
    notes: Optional[Mapping[str, Any]] = None,
    reference_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    body: dict[str, Any] = {
        "email": email,
        "phone": phone,
        "legal_business_name": legal_business_name,
        "business_type": business_type,
        "contact_name": contact_name,
        "profile": dict(profile),
        "type": "route",
    }
    if legal_info:
        body["legal_info"] = dict(legal_info)
    if brand:
        body["brand"] = dict(brand)
    if notes:
        body["notes"] = dict(notes)
    if reference_id:
        body["reference_id"] = reference_id

    client = await get_razorpay_client()
    return await client.post(
        "/v2/accounts",
        operation="route.account.create",
        json_body=body,
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )


async def fetch_linked_account(
    account_id: str, *, merchant_id: Optional[str] = None
) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v2/accounts/{account_id}",
        operation="route.account.fetch",
        merchant_id=merchant_id,
    )


async def update_linked_account(
    account_id: str,
    *,
    body: Mapping[str, Any],
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    return await client.patch(
        f"/v2/accounts/{account_id}",
        operation="route.account.update",
        json_body=dict(body),
        merchant_id=merchant_id,
    )


async def delete_linked_account(
    account_id: str, *, merchant_id: Optional[str] = None
) -> dict:
    client = await get_razorpay_client()
    return await client.delete(
        f"/v2/accounts/{account_id}",
        operation="route.account.delete",
        merchant_id=merchant_id,
    )


# ── Transfers (v1) ───────────────────────────────────────────────────────


async def create_transfers_for_payment(
    payment_id: str,
    *,
    transfers: Sequence[Mapping[str, Any]],
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    """
    Each transfer dict shape:
      {"account": "acc_xxx", "amount": <paise>, "currency": "INR",
       "notes": {...}, "linked_account_notes": [...], "on_hold": 0,
       "on_hold_until": <epoch>}
    """
    client = await get_razorpay_client()
    return await client.post(
        f"/v1/payments/{payment_id}/transfers",
        operation="route.transfers.create",
        json_body={"transfers": [dict(t) for t in transfers]},
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )


async def fetch_transfer(
    transfer_id: str, *, merchant_id: Optional[str] = None
) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/transfers/{transfer_id}",
        operation="route.transfers.fetch",
        merchant_id=merchant_id,
    )


async def list_transfers(
    *,
    count: int = 25,
    skip: int = 0,
    payment_id: Optional[str] = None,
    recipient_settlement_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    params: dict[str, Any] = {"count": count, "skip": skip}
    if payment_id:
        params["payment_id"] = payment_id
    if recipient_settlement_id:
        params["recipient_settlement_id"] = recipient_settlement_id
    return await client.get(
        "/v1/transfers",
        operation="route.transfers.list",
        params=params,
        merchant_id=merchant_id,
    )


async def reverse_transfer(
    transfer_id: str,
    *,
    amount_paise: Optional[int] = None,
    notes: Optional[Mapping[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    body: dict[str, Any] = {}
    if amount_paise is not None:
        body["amount"] = int(amount_paise)
    if notes:
        body["notes"] = dict(notes)
    client = await get_razorpay_client()
    return await client.post(
        f"/v1/transfers/{transfer_id}/reversals",
        operation="route.transfers.reverse",
        json_body=body,
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )
