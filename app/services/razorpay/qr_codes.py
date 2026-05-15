"""
Razorpay QR Codes API service (Phase 2 fills mapping + status APIs).

Phase 1 surface: REST wrappers around `/v1/payments/qr_codes`.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from app.services.razorpay.client import get_razorpay_client


async def create_qr(
    *,
    name: str,
    amount_paise: Optional[int],
    description: str = "",
    fixed_amount: bool = True,
    usage: str = "single_use",
    qr_type: str = "upi_qr",
    close_by: Optional[int] = None,
    customer_id: Optional[str] = None,
    notes: Optional[Mapping[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    body: dict[str, Any] = {
        "type": qr_type,
        "name": name,
        "usage": usage,
        "fixed_amount": fixed_amount,
        "description": description,
    }
    if fixed_amount and amount_paise is not None:
        body["payment_amount"] = int(amount_paise)
    if close_by:
        body["close_by"] = int(close_by)
    if customer_id:
        body["customer_id"] = customer_id
    if notes:
        body["notes"] = dict(notes)

    client = await get_razorpay_client()
    return await client.post(
        "/v1/payments/qr_codes",
        operation="qr.create",
        json_body=body,
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )


async def fetch_qr(qr_id: str, *, merchant_id: Optional[str] = None) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/payments/qr_codes/{qr_id}",
        operation="qr.fetch",
        merchant_id=merchant_id,
    )


async def list_qr(
    *,
    count: int = 25,
    skip: int = 0,
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        "/v1/payments/qr_codes",
        operation="qr.list",
        params={"count": count, "skip": skip},
        merchant_id=merchant_id,
    )


async def fetch_qr_payments(
    qr_id: str, *, merchant_id: Optional[str] = None
) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/payments/qr_codes/{qr_id}/payments",
        operation="qr.payments",
        merchant_id=merchant_id,
    )


async def close_qr(qr_id: str, *, merchant_id: Optional[str] = None) -> dict:
    client = await get_razorpay_client()
    return await client.post(
        f"/v1/payments/qr_codes/{qr_id}/close",
        operation="qr.close",
        json_body={},
        merchant_id=merchant_id,
    )
