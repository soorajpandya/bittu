"""Order endpoints."""
from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission, require_role
from app.services.order_service import OrderService

router = APIRouter(prefix="/orders", tags=["Orders"])
_svc = OrderService()


# ── Schemas ──────────────────────────────────────────────────

class OrderItemIn(BaseModel):
    item_name: str
    variant_name: Optional[str] = None
    quantity: int = Field(ge=1)
    notes: Optional[str] = None


class CreateOrderIn(BaseModel):
    items: list[OrderItemIn]
    table_id: Optional[str] = None
    coupon_code: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    delivery_address: Optional[str] = None
    idempotency_key: Optional[str] = None


class UpdateStatusIn(BaseModel):
    status: str


# ── Routes ───────────────────────────────────────────────────

@router.post("")
async def create_order(
    body: CreateOrderIn,
    user: UserContext = Depends(require_permission("orders.create")),
):
    return await _svc.create_order(
        user=user,
        items=[i.model_dump() for i in body.items],
        source="pos",  # Default source
        table_number=body.table_id,
        delivery_address=body.delivery_address,
        delivery_phone=body.customer_phone,
        notes=body.customer_name,  # Using customer_name as notes for now
        idempotency_key=body.idempotency_key,
    )


@router.get("")
async def list_orders(
    branch_id: Optional[str] = None,
    status: Optional[str] = None,
    order_type: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: UserContext = Depends(require_permission("orders.read")),
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
    user: UserContext = Depends(require_permission("orders.read")),
):
    return await _svc.get_order_detail(user=user, order_id=order_id)


@router.patch("/{order_id}/status")
async def update_order_status(
    order_id: str,
    body: UpdateStatusIn,
    user: UserContext = Depends(require_permission("orders.update")),
):
    return await _svc.update_status(user=user, order_id=order_id, new_status=body.status)
