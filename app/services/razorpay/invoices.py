"""
Razorpay Invoices API service (Phase 12 fills wiring).

Phase 1 surface: REST wrappers around `/v1/invoices`.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from app.services.razorpay.client import get_razorpay_client


async def create_invoice(
    *,
    amount_paise: int,
    currency: str = "INR",
    customer: Optional[Mapping[str, Any]] = None,
    customer_id: Optional[str] = None,
    description: Optional[str] = None,
    receipt: Optional[str] = None,
    line_items: Optional[Sequence[Mapping[str, Any]]] = None,
    notes: Optional[Mapping[str, Any]] = None,
    sms_notify: bool = True,
    email_notify: bool = True,
    expire_by: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    body: dict[str, Any] = {
        "type": "invoice",
        "amount": int(amount_paise),
        "currency": currency,
        "sms_notify": int(bool(sms_notify)),
        "email_notify": int(bool(email_notify)),
    }
    if customer is not None:
        body["customer"] = dict(customer)
    if customer_id:
        body["customer_id"] = customer_id
    if description:
        body["description"] = description
    if receipt:
        body["receipt"] = receipt
    if line_items:
        body["line_items"] = [dict(li) for li in line_items]
    if notes:
        body["notes"] = dict(notes)
    if expire_by:
        body["expire_by"] = int(expire_by)

    client = await get_razorpay_client()
    return await client.post(
        "/v1/invoices",
        operation="invoices.create",
        json_body=body,
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )


async def fetch_invoice(
    invoice_id: str, *, merchant_id: Optional[str] = None
) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/invoices/{invoice_id}",
        operation="invoices.fetch",
        merchant_id=merchant_id,
    )


async def list_invoices(
    *,
    count: int = 25,
    skip: int = 0,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    status: Optional[str] = None,
    receipt: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    params: dict[str, Any] = {"count": count, "skip": skip}
    if from_ts is not None:
        params["from"] = from_ts
    if to_ts is not None:
        params["to"] = to_ts
    if status:
        params["status"] = status
    if receipt:
        params["receipt"] = receipt
    return await client.get(
        "/v1/invoices",
        operation="invoices.list",
        params=params,
        merchant_id=merchant_id,
    )


async def cancel_invoice(
    invoice_id: str, *, merchant_id: Optional[str] = None
) -> dict:
    client = await get_razorpay_client()
    return await client.post(
        f"/v1/invoices/{invoice_id}/cancel",
        operation="invoices.cancel",
        json_body={},
        merchant_id=merchant_id,
    )


async def issue_invoice(
    invoice_id: str, *, merchant_id: Optional[str] = None
) -> dict:
    client = await get_razorpay_client()
    return await client.post(
        f"/v1/invoices/{invoice_id}/issue",
        operation="invoices.issue",
        json_body={},
        merchant_id=merchant_id,
    )


async def notify_invoice(
    invoice_id: str,
    *,
    medium: str = "sms",                  # sms|email
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    return await client.post(
        f"/v1/invoices/{invoice_id}/notify_by/{medium}",
        operation="invoices.notify",
        json_body={},
        merchant_id=merchant_id,
    )


async def update_invoice(
    invoice_id: str,
    *,
    body: Mapping[str, Any],
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    return await client.patch(
        f"/v1/invoices/{invoice_id}",
        operation="invoices.update",
        json_body=dict(body),
        merchant_id=merchant_id,
    )
