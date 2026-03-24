"""Kitchen Display System endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.kitchen_service import KitchenService

router = APIRouter(prefix="/kitchen", tags=["Kitchen"])
_svc = KitchenService()


class UpdateKitchenOrderStatusIn(BaseModel):
    status: str


class UpdateItemStatusIn(BaseModel):
    status: str


@router.get("/active")
async def get_active_orders(
    branch_id: Optional[str] = None,
    day_start: Optional[str] = None,
    day_end: Optional[str] = None,
    station_id: Optional[str] = None,
    status: Optional[str] = None,
    user: UserContext = Depends(require_permission("kitchen.read")),
):
    return await _svc.get_active_orders(user=user, station_id=station_id, status=status)


@router.patch("/orders/{order_id}/status")
async def update_kitchen_order_status(
    order_id: str,
    body: UpdateKitchenOrderStatusIn,
    user: UserContext = Depends(require_permission("kitchen.update")),
):
    return await _svc.update_order_status(user=user, order_id=order_id, new_status=body.status)


@router.patch("/items/{item_id}/status")
async def update_kitchen_item_status(
    item_id: str,
    body: UpdateItemStatusIn,
    user: UserContext = Depends(require_permission("kitchen.update")),
):
    return await _svc.update_item_status(user=user, item_id=item_id, new_status=body.status)


@router.get("/stations/{station_id}")
async def get_station_orders(
    station_id: str,
    branch_id: Optional[str] = None,
    user: UserContext = Depends(require_permission("kitchen.read")),
):
    return await _svc.get_station_orders(user=user, station_id=station_id)
