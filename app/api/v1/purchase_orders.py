"""Purchase Order endpoints."""
from typing import Optional
from datetime import date
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, require_role
from app.services.purchase_order_service import PurchaseOrderService

router = APIRouter(prefix="/purchase-orders", tags=["Purchase Orders"])
_svc = PurchaseOrderService()


class POItemIn(BaseModel):
    ingredient_id: Optional[str] = None
    ingredient_name: Optional[str] = None
    quantity_ordered: Optional[float] = 0
    unit: Optional[str] = None
    unit_price: Optional[float] = 0


class POCreate(BaseModel):
    supplier_name: Optional[str] = None
    supplier_contact: Optional[str] = None
    status: Optional[str] = "draft"
    notes: Optional[str] = None
    expected_delivery_date: Optional[date] = None
    total_amount: Optional[float] = 0
    items: Optional[list[POItemIn]] = []


class POUpdate(BaseModel):
    supplier_name: Optional[str] = None
    supplier_contact: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    expected_delivery_date: Optional[date] = None
    total_amount: Optional[float] = None


@router.get("")
async def list_orders(
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    try:
        return await _svc.list_orders(user, status=status, limit=limit, offset=offset)
    except Exception:
        return []


@router.get("/{po_id}")
async def get_order(
    po_id: int,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.get_order(user, po_id)


@router.post("", status_code=201)
async def create_order(
    body: POCreate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    data = body.model_dump()
    data["items"] = [i.model_dump() for i in body.items] if body.items else []
    return await _svc.create_order(user, data)


@router.patch("/{po_id}")
async def update_order(
    po_id: int,
    body: POUpdate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.update_order(user, po_id, body.model_dump(exclude_unset=True))


@router.delete("/{po_id}")
async def delete_order(
    po_id: int,
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.delete_order(user, po_id)
