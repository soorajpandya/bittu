"""PhonePe payment endpoints."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.phonepe_service import PhonePeService

router = APIRouter(prefix="/phonepe", tags=["PhonePe"])
_svc = PhonePeService()


class CreateOrderIn(BaseModel):
    merchant_order_id: str
    amount_paise: int
    redirect_url: str
    message: str = "Payment for order"


class CheckStatusIn(BaseModel):
    merchant_order_id: str


@router.post("/create-order")
async def create_phonepe_order(
    body: CreateOrderIn,
    user: UserContext = Depends(require_permission("payments.create")),
):
    return await _svc.create_order(
        merchant_order_id=body.merchant_order_id,
        amount_paise=body.amount_paise,
        redirect_url=body.redirect_url,
        message=body.message,
        udf1=user.user_id,
    )


@router.post("/check-status")
async def check_phonepe_status(
    body: CheckStatusIn,
    user: UserContext = Depends(require_permission("payments.create")),
):
    return await _svc.check_status(body.merchant_order_id)
