"""Zivonpay payment endpoints."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.zivonpay_service import ZivonpayService

router = APIRouter(prefix="/zivonpay", tags=["Zivonpay"])
_svc = ZivonpayService()


class CreateOrderIn(BaseModel):
    order_id: str
    amount: float
    description: str = ""
    customer_name: str
    customer_phone: str
    return_url: str


@router.post("/create-order")
async def create_zivonpay_order(
    body: CreateOrderIn,
    user: UserContext = Depends(require_permission("payments.create")),
):
    return await _svc.create_order(
        order_id=body.order_id,
        amount=body.amount,
        description=body.description,
        customer_name=body.customer_name,
        customer_phone=body.customer_phone,
        return_url=body.return_url,
    )
