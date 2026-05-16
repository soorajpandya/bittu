"""
Order API endpoints.

Endpoints
---------
POST  /orders/checkout       — Idempotent POS checkout (create + full response).
POST  /orders                — Legacy create order (delegates to checkout logic).
GET   /orders                — Paginated list, newest-first, inline items (no N+1).
GET   /orders/{order_id}     — Full order detail with items[].
PATCH /orders/{order_id}/status — Status transition.
PATCH /orders/{order_id}     — Update notes / status.
PUT   /orders/{order_id}     — Same as PATCH (alternative verb).
DELETE /orders/{order_id}    — Cancel order.
POST  /orders/{order_id}/discount — Apply percentage discount.

Date/time contract
------------------
from_date / to_date are interpreted as UTC calendar days.
  from_date inclusive → created_at >= from_date (00:00 UTC)
  to_date   inclusive → created_at <  to_date + 1 day (00:00 UTC next day)

Idempotency
-----------
POST /checkout accepts idempotency key from:
  Header: X-Idempotency-Key   (takes precedence)
  Body:   idempotency_key
Same key + same auth scope will never produce a duplicate order.
"""
from datetime import date
from typing import Optional, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field, model_validator

from app.core.auth import UserContext, require_permission
from app.core.exceptions import ValidationError
from app.services.order_service import OrderService
from app.services.activity_log_service import log_activity

router = APIRouter(prefix="/orders", tags=["Orders"])
_svc = OrderService()


# ── Schemas ──────────────────────────────────────────────────

class OrderItemIn(BaseModel):
    model_config = {"extra": "allow"}

    item_id: Optional[Any] = None
    item_name: Optional[str] = None
    variant_id: Optional[Any] = None
    variant_name: Optional[str] = None
    quantity: int = Field(default=1, ge=1)
    price: Optional[float] = None  # Client hint; server recalculates
    addons: Optional[list] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def require_item_id_or_name(self):
        if not self.item_id and not self.item_name:
            raise ValueError("Either item_id or item_name is required")
        return self


class CheckoutIn(BaseModel):
    """
    POST /orders/checkout — POS checkout payload.

    Required: items, total_amount, payment_method, order_type, source.
    Optional: everything else.

    Idempotency key can also be supplied via the X-Idempotency-Key header
    (header takes precedence over body field).
    """
    model_config = {"extra": "allow"}

    # Required fields
    items: list[OrderItemIn]
    total_amount: float = Field(description="Client-submitted total (for logging/audit; server recalculates)")
    payment_method: str = Field(description="cash | upi | card | wallet | online")
    order_type: str = Field(description="pos | dine_in | takeaway | delivery")
    source: str = Field(default="pos", description="Request source identifier")

    # Optional
    subtotal: Optional[float] = None
    discount_amount: Optional[float] = None
    tax_amount: Optional[float] = None
    service_charge: Optional[float] = None
    customer_id: Optional[int] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    delivery_address: Optional[str] = None
    notes: Optional[str] = None
    branch_id: Optional[str] = None
    table_id: Optional[str] = None
    table_number: Optional[str] = None
    coupon_id: Optional[int] = None
    coupon_code: Optional[str] = None
    idempotency_key: Optional[str] = None  # body fallback; header takes precedence
    # Customer device GPS — used to enforce per-branch geofence (see
    # app.core.geofence). Optional; when omitted the check is skipped.
    customer_lat: Optional[float] = None
    customer_lng: Optional[float] = None


class CreateOrderIn(BaseModel):
    model_config = {"extra": "allow"}

    items: list[OrderItemIn]
    order_type: Optional[str] = None  # dine_in, takeaway, delivery
    table_id: Optional[str] = None
    table_number: Optional[str] = None
    branch_id: Optional[str] = None
    coupon_code: Optional[str] = None
    coupon_id: Optional[int] = None
    customer_id: Optional[int] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    delivery_address: Optional[str] = None
    notes: Optional[str] = None
    source: Optional[str] = "pos"
    idempotency_key: Optional[str] = None


class UpdateStatusIn(BaseModel):
    status: str


class UpdateOrderIn(BaseModel):
    model_config = {"extra": "allow"}
    status: Optional[str] = None
    notes: Optional[str] = None


class ApplyDiscountIn(BaseModel):
    discount_percent: float = Field(gt=0, le=100)
    reason: Optional[str] = None


# ── Routes ───────────────────────────────────────────────────

@router.post(
    "/checkout",
    summary="Idempotent POS checkout",
    description=(
        "Create an order atomically and return the full committed order including items[]. "
        "Safe to retry: same X-Idempotency-Key + same auth scope never creates a duplicate. "
        "Replayed responses carry `idempotent: true`."
    ),
)
async def checkout_order(
    body: CheckoutIn,
    user: UserContext = Depends(require_permission("order.create")),
    x_idempotency_key: Optional[str] = Header(default=None, alias="X-Idempotency-Key"),
):
    # Header takes precedence over body field
    idem_key = x_idempotency_key or body.idempotency_key

    # Geo-fence: enforce only when the merchant opted in on this branch.
    # No-op when branch/lat/lng are unset.
    from app.core.geofence import assert_within_geofence
    await assert_within_geofence(
        merchant_id=str(user.restaurant_id),
        branch_id=body.branch_id or (str(user.branch_id) if user.branch_id else None),
        customer_lat=body.customer_lat,
        customer_lng=body.customer_lng,
    )

    result = await _svc.checkout(
        user=user,
        items=[i.model_dump(exclude_none=True) for i in body.items],
        source=body.source,
        order_type=body.order_type,
        payment_method=body.payment_method,
        total_amount=body.total_amount,
        customer_id=body.customer_id,
        customer_name=body.customer_name,
        customer_phone=body.customer_phone,
        table_number=body.table_id or body.table_number,
        delivery_address=body.delivery_address,
        coupon_id=body.coupon_id,
        coupon_code=body.coupon_code,
        notes=body.notes,
        idempotency_key=idem_key,
    )

    if not result.get("idempotent"):
        await log_activity(
            user_id=user.user_id,
            branch_id=user.branch_id,
            action="order.checkout",
            entity_type="order",
            entity_id=result.get("id"),
            metadata={
                "source": body.source,
                "order_type": body.order_type,
                "payment_method": body.payment_method,
                "item_count": len(body.items),
                "idempotency_key": idem_key,
            },
        )

    return result


@router.post("", summary="Create order (legacy)")
async def create_order(
    body: CreateOrderIn,
    user: UserContext = Depends(require_permission("order.create")),
    x_idempotency_key: Optional[str] = Header(default=None, alias="X-Idempotency-Key"),
):
    idem_key = x_idempotency_key or body.idempotency_key

    result = await _svc.create_order(
        user=user,
        items=[i.model_dump(exclude_none=True) for i in body.items],
        source=body.source or "pos",
        customer_id=body.customer_id,
        table_number=body.table_id or body.table_number,
        delivery_address=body.delivery_address,
        delivery_phone=body.customer_phone,
        coupon_id=body.coupon_id,
        notes=body.notes or body.customer_name,
        idempotency_key=idem_key,
    )
    await log_activity(
        user_id=user.user_id,
        branch_id=user.branch_id,
        action="order.created",
        entity_type="order",
        entity_id=result.get("id"),
        metadata={"source": body.source or "pos", "item_count": len(body.items)},
    )
    return result


@router.get(
    "",
    summary="List orders (paginated, newest-first, inline items)",
    description=(
        "Returns orders sorted by created_at DESC. "
        "Each order includes items[] inline — no per-order detail fetch required. "
        "Date filters are UTC calendar days (from_date inclusive, to_date inclusive)."
    ),
)
async def list_orders(
    branch_id: Optional[str] = None,
    status: Optional[str] = None,
    order_type: Optional[str] = None,
    from_date: Optional[date] = Query(default=None, description="UTC calendar date, inclusive lower bound"),
    to_date: Optional[date] = Query(default=None, description="UTC calendar date, inclusive upper bound"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    include_items: bool = Query(True, description="Embed order items inline (set false to reduce payload)"),
    user: UserContext = Depends(require_permission("order.read")),
):
    offset = (page - 1) * page_size
    return await _svc.get_orders(
        user=user,
        branch_id=branch_id,
        status=status,
        order_type=order_type,
        from_date=str(from_date) if from_date else None,
        to_date=str(to_date) if to_date else None,
        limit=page_size,
        offset=offset,
        include_items=include_items,
    )


@router.get("/{order_id}", summary="Get order detail with items[]")
async def get_order(
    order_id: str,
    user: UserContext = Depends(require_permission("order.read")),
):
    return await _svc.get_order_detail(user=user, order_id=order_id)


@router.patch("/{order_id}/status")
async def update_order_status(
    order_id: str,
    body: UpdateStatusIn,
    user: UserContext = Depends(require_permission("order.edit")),
):
    return await _svc.update_status(user=user, order_id=order_id, new_status=body.status)


@router.patch("/{order_id}")
async def update_order(
    order_id: str,
    body: UpdateOrderIn,
    user: UserContext = Depends(require_permission("order.edit")),
):
    """Update order fields (status, notes, etc.)."""
    return await _svc.update_order(
        user=user, order_id=order_id, status=body.status, notes=body.notes,
    )


@router.put("/{order_id}")
async def put_order(
    order_id: str,
    body: UpdateOrderIn,
    user: UserContext = Depends(require_permission("order.edit")),
):
    """Update order fields (status, notes, etc.)."""
    return await _svc.update_order(
        user=user, order_id=order_id, status=body.status, notes=body.notes,
    )


@router.delete("/{order_id}")
async def delete_order(
    order_id: str,
    user: UserContext = Depends(require_permission("order.cancel")),
):
    """Cancel/delete an order by transitioning it to Cancelled status."""
    result = await _svc.update_status(user=user, order_id=order_id, new_status="Cancelled")
    await log_activity(
        user_id=user.user_id,
        branch_id=user.branch_id,
        action="order.cancelled",
        entity_type="order",
        entity_id=order_id,
        metadata={"status": "Cancelled"},
    )
    return result


@router.post("/{order_id}/discount")
async def apply_discount(
    order_id: str,
    body: ApplyDiscountIn,
    user: UserContext = Depends(require_permission("billing.discount")),
):
    max_discount = None
    if user.permission_meta:
        max_discount = user.permission_meta.get("max_discount_percent")

    if max_discount is not None and body.discount_percent > float(max_discount):
        raise ValidationError(
            f"Discount {body.discount_percent}% exceeds max allowed {max_discount}% for your role"
        )

    result = await _svc.apply_discount(
        user=user,
        order_id=order_id,
        discount_percent=body.discount_percent,
        reason=body.reason,
    )
    await log_activity(
        user_id=user.user_id,
        branch_id=user.branch_id,
        action="billing.discount_applied",
        entity_type="order",
        entity_id=order_id,
        metadata={"discount_percent": body.discount_percent, "reason": body.reason},
    )
    return result
