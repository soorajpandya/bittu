"""Order endpoints."""
from datetime import date
from typing import Optional, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
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

@router.post("")
async def create_order(
    body: CreateOrderIn,
    user: UserContext = Depends(require_permission("order.create")),
):
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
        idempotency_key=body.idempotency_key,
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


@router.get("")
async def list_orders(
    branch_id: Optional[str] = None,
    status: Optional[str] = None,
    order_type: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
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
    )


@router.get("/{order_id}")
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
