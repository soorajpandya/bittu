"""Cashfree PG payment endpoints."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.cashfree_pg_service import CashfreeService

router = APIRouter(prefix="/cashfree", tags=["Cashfree PG"])
_svc = CashfreeService()


class CreateOrderIn(BaseModel):
    order_id: str
    order_amount: float
    customer_id: str
    customer_name: str
    customer_phone: str
    return_url: str


@router.post("/create-order")
async def create_cashfree_order(
    body: CreateOrderIn,
    user: UserContext = Depends(require_permission("payments.create")),
):
    return await _svc.create_order(
        order_id=body.order_id,
        order_amount=body.order_amount,
        customer_id=body.customer_id,
        customer_name=body.customer_name,
        customer_phone=body.customer_phone,
        return_url=body.return_url,
    )
