"""
Razorpay Route — raw passthrough endpoints (Postman parity).

These endpoints mirror Razorpay's Route API surface 1:1 in path, method and
payload shape. Bodies are forwarded verbatim to Razorpay so callers can use
the official Razorpay docs / Postman collection without any reshaping.

Auth / merchant-key resolution still goes through the shared
`RazorpayClient` (basic auth with key_id / key_secret), and every route is
gated by the same RBAC scopes used elsewhere in `app/api/v1/rzp_route.py`:
    razorpay.route.read   — GET endpoints
    razorpay.route.write  — POST / PATCH / DELETE endpoints

The 22 endpoints below cover the full Razorpay Route Postman collection:
    1.  Create Transfers From Orders         POST   /orders
    2.  Create Transfers From Payments       POST   /payments/{payment_id}/transfers
    3.  Create Direct Transfer               POST   /transfers
    4.  Fetch Transfers for a Payment        GET    /payments/{payment_id}/transfers
    5.  Fetch Transfers for an Order         GET    /orders/{order_id}/transfers
    6.  Fetch a Transfer                     GET    /transfers/{transfer_id}
    7.  Fetch Transfers for a Settlement     GET    /settlements/{settlement_id}/transfers
    8.  Fetch Settlement Details             GET    /transfers/{transfer_id}/settlement-details
    9.  Fetch Payments of a Linked Account   GET    /accounts/{account_id}/payments
    10. Fetch Reversals for a Transfer       GET    /transfers/{transfer_id}/reversals
    11. Modify Transfer Settlement Hold      PATCH  /transfers/{transfer_id}
    12. Hold Settlements For Transfers       POST   /payments/{payment_id}/transfers/hold
    13. Refunds (with reverse_all)           POST   /payments/{payment_id}/refund
    14. Transfer Reversals                   POST   /transfers/{transfer_id}/reversals
    15. Create a Linked Account              POST   /accounts
    16. Update a Linked Account              PATCH  /accounts/{account_id}
    17. Fetch a Linked Account               GET    /accounts/{account_id}
    18. Create a Stakeholder                 POST   /accounts/{account_id}/stakeholders
    19. Update a Stakeholder                 PATCH  /accounts/{account_id}/stakeholders/{stakeholder_id}
    20. Request Product Configuration        POST   /accounts/{account_id}/products
    21. Update Product Configuration         PATCH  /accounts/{account_id}/products/{product_id}
    22. Fetch Product Configuration          GET    /accounts/{account_id}/products/{product_id}
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query

from app.core.auth import UserContext, require_permission
from app.services.razorpay import route as _route


router = APIRouter(prefix="/razorpay-route/raw", tags=["razorpay-route-raw"])


# ── helpers ──────────────────────────────────────────────────────────────


def _mid(user: UserContext) -> str:
    """Resolve the caller's merchant id (used for per-merchant key lookup)."""
    rid = getattr(user, "restaurant_id", None)
    if rid is None:
        raise HTTPException(status_code=400, detail="merchant context missing")
    return str(rid)


# ── 1. Create Transfers From Orders ──────────────────────────────────────
@router.post("/orders")
async def create_order_with_transfers(
    body: dict = Body(..., description="Razorpay /v1/orders payload incl. transfers[]"),
    idempotency_key: Optional[str] = Header(default=None, alias="X-Idempotency-Key"),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    return await _route.create_order_with_transfers(
        body=body,
        idempotency_key=idempotency_key,
        merchant_id=_mid(user),
    )


# ── 2. Create Transfers From Payments ────────────────────────────────────
@router.post("/payments/{payment_id}/transfers")
async def create_transfers_for_payment(
    payment_id: str,
    body: dict = Body(..., description='Razorpay payload: {"transfers": [...]}'),
    idempotency_key: Optional[str] = Header(default=None, alias="X-Idempotency-Key"),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    transfers = body.get("transfers") or []
    return await _route.create_transfers_for_payment(
        payment_id,
        transfers=transfers,
        idempotency_key=idempotency_key,
        merchant_id=_mid(user),
    )


# ── 3. Create Direct Transfer ────────────────────────────────────────────
@router.post("/transfers")
async def create_direct_transfer(
    body: dict = Body(..., description="Razorpay /v1/transfers payload"),
    idempotency_key: Optional[str] = Header(default=None, alias="X-Idempotency-Key"),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    return await _route.create_direct_transfer(
        body=body,
        idempotency_key=idempotency_key,
        merchant_id=_mid(user),
    )


# ── 4. Fetch Transfers for a Payment ─────────────────────────────────────
@router.get("/payments/{payment_id}/transfers")
async def fetch_transfers_for_payment(
    payment_id: str,
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    return await _route.fetch_transfers_for_payment(
        payment_id, merchant_id=_mid(user)
    )


# ── 5. Fetch Transfers for an Order ──────────────────────────────────────
@router.get("/orders/{order_id}/transfers")
async def fetch_transfers_for_order(
    order_id: str,
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    return await _route.fetch_transfers_for_order(
        order_id, merchant_id=_mid(user)
    )


# ── 6. Fetch a Transfer ──────────────────────────────────────────────────
@router.get("/transfers/{transfer_id}")
async def fetch_transfer(
    transfer_id: str,
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    return await _route.fetch_transfer(transfer_id, merchant_id=_mid(user))


# ── 7. Fetch Transfers for a Settlement ──────────────────────────────────
@router.get("/settlements/{settlement_id}/transfers")
async def fetch_transfers_for_settlement(
    settlement_id: str,
    count: int = Query(default=25, ge=1, le=100),
    skip: int = Query(default=0, ge=0),
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    return await _route.list_transfers(
        count=count,
        skip=skip,
        recipient_settlement_id=settlement_id,
        merchant_id=_mid(user),
    )


# ── 8. Fetch Settlement Details for a Transfer ───────────────────────────
@router.get("/transfers/{transfer_id}/settlement-details")
async def fetch_settlement_details(
    transfer_id: str,
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    return await _route.fetch_transfer_with_settlement(
        transfer_id, merchant_id=_mid(user)
    )


# ── 9. Fetch Payments of a Linked Account ────────────────────────────────
@router.get("/accounts/{account_id}/payments")
async def fetch_payments_for_linked_account(
    account_id: str,
    count: int = Query(default=25, ge=1, le=100),
    skip: int = Query(default=0, ge=0),
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    return await _route.fetch_payments_for_linked_account(
        account_id, count=count, skip=skip, merchant_id=_mid(user)
    )


# ── 10. Fetch Reversals for a Transfer ───────────────────────────────────
@router.get("/transfers/{transfer_id}/reversals")
async def fetch_reversals_for_transfer(
    transfer_id: str,
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    return await _route.fetch_reversals_for_transfer(
        transfer_id, merchant_id=_mid(user)
    )


# ── 11. Modify Transfer Settlement Hold ──────────────────────────────────
@router.patch("/transfers/{transfer_id}")
async def modify_transfer(
    transfer_id: str,
    body: dict = Body(..., description='Razorpay PATCH /v1/transfers/{id}, e.g. {"on_hold": 1, "on_hold_until": ...}'),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    return await _route.modify_transfer(
        transfer_id, body=body, merchant_id=_mid(user)
    )


# ── 12. Hold Settlements For Transfers ───────────────────────────────────
# Razorpay reuses POST /v1/payments/{id}/transfers with `on_hold`/`on_hold_until`
# inside each transfer; exposed under a distinct path for clarity.
@router.post("/payments/{payment_id}/transfers/hold")
async def hold_settlements_for_transfers(
    payment_id: str,
    body: dict = Body(
        ...,
        description='Razorpay payload: {"transfers": [{"account": ..., "amount": ..., "currency": ..., "on_hold": 1, "on_hold_until": ...}]}',
    ),
    idempotency_key: Optional[str] = Header(default=None, alias="X-Idempotency-Key"),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    transfers = body.get("transfers") or []
    return await _route.create_transfers_for_payment(
        payment_id,
        transfers=transfers,
        idempotency_key=idempotency_key,
        merchant_id=_mid(user),
    )


# ── 13. Refunds (with reverse_all) ───────────────────────────────────────
@router.post("/payments/{payment_id}/refund")
async def refund_payment(
    payment_id: str,
    body: dict = Body(
        ...,
        description='Razorpay /v1/payments/{id}/refund payload, e.g. {"amount": 1000, "reverse_all": 1}',
    ),
    idempotency_key: Optional[str] = Header(default=None, alias="X-Idempotency-Key"),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    return await _route.refund_payment(
        payment_id,
        body=body,
        idempotency_key=idempotency_key,
        merchant_id=_mid(user),
    )


# ── 14. Transfer Reversals ───────────────────────────────────────────────
@router.post("/transfers/{transfer_id}/reversals")
async def reverse_transfer(
    transfer_id: str,
    body: dict = Body(
        default_factory=dict,
        description='Razorpay payload: {"amount": <paise>, "notes": {...}}',
    ),
    idempotency_key: Optional[str] = Header(default=None, alias="X-Idempotency-Key"),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    amount = body.get("amount")
    notes = body.get("notes")
    return await _route.reverse_transfer(
        transfer_id,
        amount_paise=int(amount) if amount is not None else None,
        notes=notes,
        idempotency_key=idempotency_key,
        merchant_id=_mid(user),
    )


# ── 15. Create a Linked Account ──────────────────────────────────────────
@router.post("/accounts")
async def create_linked_account(
    body: dict = Body(..., description="Razorpay POST /v2/accounts payload"),
    idempotency_key: Optional[str] = Header(default=None, alias="X-Idempotency-Key"),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    payload = dict(body)
    payload.setdefault("type", "route")
    return await _route.create_linked_account(
        email=payload.get("email"),
        phone=payload.get("phone"),
        legal_business_name=payload.get("legal_business_name"),
        business_type=payload.get("business_type"),
        contact_name=payload.get("contact_name"),
        profile=payload.get("profile") or {},
        legal_info=payload.get("legal_info"),
        brand=payload.get("brand"),
        notes=payload.get("notes"),
        reference_id=payload.get("reference_id"),
        idempotency_key=idempotency_key,
        merchant_id=_mid(user),
    )


# ── 16. Update a Linked Account ──────────────────────────────────────────
@router.patch("/accounts/{account_id}")
async def update_linked_account(
    account_id: str,
    body: dict = Body(..., description="Razorpay PATCH /v2/accounts/{id} payload"),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    return await _route.update_linked_account(
        account_id, body=body, merchant_id=_mid(user)
    )


# ── 17. Fetch a Linked Account ───────────────────────────────────────────
@router.get("/accounts/{account_id}")
async def fetch_linked_account(
    account_id: str,
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    return await _route.fetch_linked_account(account_id, merchant_id=_mid(user))


# ── 18. Create a Stakeholder ─────────────────────────────────────────────
@router.post("/accounts/{account_id}/stakeholders")
async def create_stakeholder(
    account_id: str,
    body: dict = Body(..., description="Razorpay POST /v2/accounts/{id}/stakeholders payload"),
    idempotency_key: Optional[str] = Header(default=None, alias="X-Idempotency-Key"),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    return await _route.create_stakeholder(
        account_id,
        body=body,
        idempotency_key=idempotency_key,
        merchant_id=_mid(user),
    )


# ── 19. Update a Stakeholder ─────────────────────────────────────────────
@router.patch("/accounts/{account_id}/stakeholders/{stakeholder_id}")
async def update_stakeholder(
    account_id: str,
    stakeholder_id: str,
    body: dict = Body(..., description="Razorpay PATCH /v2/accounts/{id}/stakeholders/{sid} payload"),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    return await _route.update_stakeholder(
        account_id, stakeholder_id, body=body, merchant_id=_mid(user)
    )


# ── 20. Request Product Configuration ────────────────────────────────────
@router.post("/accounts/{account_id}/products")
async def request_product_configuration(
    account_id: str,
    body: dict = Body(..., description="Razorpay POST /v2/accounts/{id}/products payload"),
    idempotency_key: Optional[str] = Header(default=None, alias="X-Idempotency-Key"),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    return await _route.request_product_configuration(
        account_id,
        body=body,
        idempotency_key=idempotency_key,
        merchant_id=_mid(user),
    )


# ── 21. Update Product Configuration ─────────────────────────────────────
@router.patch("/accounts/{account_id}/products/{product_id}")
async def update_product_configuration(
    account_id: str,
    product_id: str,
    body: dict = Body(..., description="Razorpay PATCH /v2/accounts/{id}/products/{pid} payload"),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    return await _route.update_product_configuration(
        account_id, product_id, body=body, merchant_id=_mid(user)
    )


# ── 22. Fetch Product Configuration ──────────────────────────────────────
@router.get("/accounts/{account_id}/products/{product_id}")
async def fetch_product_configuration(
    account_id: str,
    product_id: str,
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    return await _route.fetch_product_configuration(
        account_id, product_id, merchant_id=_mid(user)
    )
