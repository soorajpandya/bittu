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
    customer_facing_business_name: Optional[str] = None,
    contact_info: Optional[Mapping[str, Any]] = None,
    apps: Optional[Mapping[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    """Wraps ``POST /v2/accounts``. See Razorpay Route docs for the full
    field reference. All optional pass-throughs are sent only if the
    caller supplied a truthy value; the service layer is responsible for
    validating/normalising values before they reach us.
    """
    body: dict[str, Any] = {
        "email": email,
        "phone": phone,
        "legal_business_name": legal_business_name,
        "business_type": business_type,
        "contact_name": contact_name,
        "profile": dict(profile),
        "type": "route",
    }
    if customer_facing_business_name:
        body["customer_facing_business_name"] = customer_facing_business_name
    if legal_info:
        body["legal_info"] = dict(legal_info)
    if brand:
        body["brand"] = dict(brand)
    if notes:
        body["notes"] = dict(notes)
    if reference_id:
        body["reference_id"] = reference_id
    if contact_info:
        body["contact_info"] = dict(contact_info)
    if apps:
        body["apps"] = dict(apps)

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


# ── Stakeholders (v2) ────────────────────────────────────────────────────
#
# Step 3 of Route onboarding: every linked account needs at least one
# stakeholder before a product configuration can be requested.


async def create_stakeholder(
    account_id: str,
    *,
    body: Mapping[str, Any],
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    """POST /v2/accounts/{account_id}/stakeholders"""
    client = await get_razorpay_client()
    return await client.post(
        f"/v2/accounts/{account_id}/stakeholders",
        operation="route.stakeholder.create",
        json_body=dict(body),
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )


async def fetch_stakeholder(
    account_id: str,
    stakeholder_id: str,
    *,
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v2/accounts/{account_id}/stakeholders/{stakeholder_id}",
        operation="route.stakeholder.fetch",
        merchant_id=merchant_id,
    )


async def fetch_all_stakeholders(
    account_id: str, *, merchant_id: Optional[str] = None
) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v2/accounts/{account_id}/stakeholders",
        operation="route.stakeholder.list",
        merchant_id=merchant_id,
    )


async def update_stakeholder(
    account_id: str,
    stakeholder_id: str,
    *,
    body: Mapping[str, Any],
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    return await client.patch(
        f"/v2/accounts/{account_id}/stakeholders/{stakeholder_id}",
        operation="route.stakeholder.update",
        json_body=dict(body),
        merchant_id=merchant_id,
    )


# ── Product configuration (v2) ───────────────────────────────────────────
#
# Steps 4 & 5 of Route onboarding: request the `route` product, then update
# it with the merchant's settlement bank details. The product configuration
# transitions to `activated` once Razorpay reviews the bank details. The
# concrete implementations live further down in this module (body=-based);
# the duplicate kwargs-style defs that used to sit here were shadowed at
# import time and only surfaced as confusing TypeErrors when someone
# called them with the kwargs signature.


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


# ── Raw passthroughs (Razorpay Route Postman parity) ─────────────────────
#
# These helpers forward the request body / query params as-is to Razorpay.
# They intentionally do NOT reshape payloads so callers can match Razorpay
# docs verbatim. Auth + merchant key resolution still goes through the
# shared RazorpayClient (basic auth with key_id/key_secret).


async def create_order_with_transfers(
    *,
    body: Mapping[str, Any],
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    """POST /v1/orders — body includes `transfers` array (Route)."""
    client = await get_razorpay_client()
    return await client.post(
        "/v1/orders",
        operation="route.order.create_with_transfers",
        json_body=dict(body),
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )


async def create_direct_transfer(
    *,
    body: Mapping[str, Any],
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    """POST /v1/transfers — direct transfer to a linked account."""
    client = await get_razorpay_client()
    return await client.post(
        "/v1/transfers",
        operation="route.transfers.direct",
        json_body=dict(body),
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )


async def fetch_transfers_for_payment(
    payment_id: str,
    *,
    merchant_id: Optional[str] = None,
) -> dict:
    """GET /v1/payments/{payment_id}/transfers."""
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/payments/{payment_id}/transfers",
        operation="route.transfers.for_payment",
        merchant_id=merchant_id,
    )


async def fetch_transfers_for_order(
    order_id: str,
    *,
    merchant_id: Optional[str] = None,
) -> dict:
    """GET /v1/orders/{order_id}/transfers."""
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/orders/{order_id}/transfers",
        operation="route.transfers.for_order",
        merchant_id=merchant_id,
    )


async def fetch_transfer_with_settlement(
    transfer_id: str,
    *,
    merchant_id: Optional[str] = None,
) -> dict:
    """GET /v1/transfers/{id}?expand[]=recipient_settlement."""
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/transfers/{transfer_id}",
        operation="route.transfers.settlement_details",
        params={"expand[]": "recipient_settlement"},
        merchant_id=merchant_id,
    )


async def fetch_payments_for_linked_account(
    account_id: str,
    *,
    count: int = 25,
    skip: int = 0,
    merchant_id: Optional[str] = None,
) -> dict:
    """GET /v1/payments under X-Razorpay-Account header (Route)."""
    client = await get_razorpay_client()
    return await client.get(
        "/v1/payments",
        operation="route.la.payments",
        params={"count": count, "skip": skip},
        merchant_id=merchant_id,
        account_id=account_id,
    )


async def fetch_reversals_for_transfer(
    transfer_id: str,
    *,
    merchant_id: Optional[str] = None,
) -> dict:
    """GET /v1/transfers/{transfer_id}/reversals."""
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/transfers/{transfer_id}/reversals",
        operation="route.transfers.reversals.list",
        merchant_id=merchant_id,
    )


async def modify_transfer(
    transfer_id: str,
    *,
    body: Mapping[str, Any],
    merchant_id: Optional[str] = None,
) -> dict:
    """PATCH /v1/transfers/{transfer_id} — modify settlement hold."""
    client = await get_razorpay_client()
    return await client.patch(
        f"/v1/transfers/{transfer_id}",
        operation="route.transfers.modify",
        json_body=dict(body),
        merchant_id=merchant_id,
    )


async def refund_payment(
    payment_id: str,
    *,
    body: Mapping[str, Any],
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    """POST /v1/payments/{payment_id}/refund — supports reverse_all=1."""
    client = await get_razorpay_client()
    return await client.post(
        f"/v1/payments/{payment_id}/refund",
        operation="route.payments.refund",
        json_body=dict(body),
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )


# ── Product configuration (v2) ───────────────────────────────────────────


async def request_product_configuration(
    account_id: str,
    *,
    body: Mapping[str, Any],
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    """POST /v2/accounts/{account_id}/products."""
    client = await get_razorpay_client()
    return await client.post(
        f"/v2/accounts/{account_id}/products",
        operation="route.product.request",
        json_body=dict(body),
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )


async def update_product_configuration(
    account_id: str,
    product_id: str,
    *,
    body: Mapping[str, Any],
    merchant_id: Optional[str] = None,
) -> dict:
    """PATCH /v2/accounts/{account_id}/products/{product_id}."""
    client = await get_razorpay_client()
    return await client.patch(
        f"/v2/accounts/{account_id}/products/{product_id}",
        operation="route.product.update",
        json_body=dict(body),
        merchant_id=merchant_id,
    )


async def fetch_product_configuration(
    account_id: str,
    product_id: str,
    *,
    merchant_id: Optional[str] = None,
) -> dict:
    """GET /v2/accounts/{account_id}/products/{product_id}."""
    client = await get_razorpay_client()
    return await client.get(
        f"/v2/accounts/{account_id}/products/{product_id}",
        operation="route.product.fetch",
        merchant_id=merchant_id,
    )


# ── Balance probe (activation liveness check) ──────────────────────────
#
# Razorpay exposes two parallel onboarding flows for Route linked
# accounts:
#
# 1. Direct V2 API (POST /v2/accounts → stakeholders → products → bank).
#    Activation state is observable via GET /v2/accounts/{id}.status.
#
# 2. Dashboard-managed batch CSV upload. The accounts created via this
#    flow are FULLY ACTIVATED by Razorpay's internal review process,
#    but the V2 introspection endpoints are SEALED for them: GET
#    /v2/accounts/{id}.status stays ``created`` forever, /products
#    returns 404 "no Route matched", and PATCH/POST return
#    ``BAD_REQUEST_ERROR: Merchant activation form has been locked for
#    editing by admin.``
#
# The ONLY API-visible signal that a batch-flow account is actually
# transfer-ready is the shape of ``GET /v1/balance`` with the
# ``X-Razorpay-Account`` header set:
#
#   Activated:     {"id":..,"merchant_id":..,"type":"primary","currency":..,"balance":..,"updated_at":..}
#   Not activated: {"id":..,"balance":0,"credits":0,"fee_credits":0,"refund_credits":0}
#
# We use the presence of the ``type`` field as the activation
# discriminator (verified May 26, 2026 against 7 live accounts —
# 5 batch-CSV activated returned full shape, 2 direct-V2 pending
# returned the 4-field stub).


async def fetch_account_balance(
    account_id: str, *, merchant_id: Optional[str] = None
) -> dict:
    """GET /v1/balance with ``X-Razorpay-Account: <account_id>``.

    Returns the raw response body. Use ``balance_indicates_activated``
    to interpret the shape.
    """
    client = await get_razorpay_client()
    return await client.get(
        "/v1/balance",
        operation="route.account.balance",
        merchant_id=merchant_id,
        account_id=account_id,
    )


def balance_indicates_activated(balance_body: Mapping[str, Any]) -> bool:
    """True when the /v1/balance response shape matches an activated
    Route linked account (has a ``type`` field — stub responses for
    not-yet-activated accounts omit it)."""
    if not isinstance(balance_body, Mapping):
        return False
    # ``type`` is the most distinctive field; ``merchant_id`` and
    # ``updated_at`` correlate but ``type`` is the cleanest signal.
    return bool(balance_body.get("type"))
