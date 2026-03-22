"""Paytm payment endpoints."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.paytm_service import PaytmService

router = APIRouter(prefix="/paytm", tags=["Paytm"])
_svc = PaytmService()


class InitiateIn(BaseModel):
    order_id: str
    amount: str
    cust_id: str
    callback_url: str


@router.post("/initiate")
async def initiate_paytm_transaction(
    body: InitiateIn,
    user: UserContext = Depends(require_permission("payments.create")),
):
    return await _svc.initiate_transaction(
        order_id=body.order_id,
        amount=body.amount,
        cust_id=body.cust_id,
        callback_url=body.callback_url,
    )
