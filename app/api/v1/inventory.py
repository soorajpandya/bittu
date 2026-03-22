"""Inventory Management endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.inventory_service import InventoryService

router = APIRouter(prefix="/inventory", tags=["Inventory"])
_svc = InventoryService()


class ReceivePurchaseIn(BaseModel):
    purchase_order_id: str


@router.get("/stock")
async def get_stock_levels(
    branch_id: str,
    low_only: bool = False,
    user: UserContext = Depends(require_permission("inventory.read")),
):
    return await _svc.get_stock_levels(user=user, branch_id=branch_id, low_only=low_only)


@router.post("/receive")
async def receive_purchase_order(
    body: ReceivePurchaseIn,
    user: UserContext = Depends(require_permission("inventory.manage")),
):
    return await _svc.receive_purchase_order(user=user, purchase_order_id=body.purchase_order_id)
