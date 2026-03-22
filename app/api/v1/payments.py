"""Payment endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.payment_service import PaymentService

router = APIRouter(prefix="/payments", tags=["Payments"])
_svc = PaymentService()


class InitiatePaymentIn(BaseModel):
    order_id: str
    payment_mode: str  # cash | online
    amount: float
    tip: float = 0


class VerifyPaymentIn(BaseModel):
    order_id: str
    razorpay_payment_id: str
    razorpay_order_id: str
    razorpay_signature: str


class RefundIn(BaseModel):
    payment_id: str
    amount: Optional[float] = None
    reason: Optional[str] = None


@router.post("/initiate")
async def initiate_payment(
    body: InitiatePaymentIn,
    user: UserContext = Depends(require_permission("payments.create")),
):
    return await _svc.initiate_payment(
        user=user,
        order_id=body.order_id,
        payment_mode=body.payment_mode,
        amount=body.amount,
        tip=body.tip,
    )


@router.post("/verify")
async def verify_payment(
    body: VerifyPaymentIn,
    user: UserContext = Depends(require_permission("payments.create")),
):
    return await _svc.verify_razorpay_payment(
        razorpay_payment_id=body.razorpay_payment_id,
        razorpay_order_id=body.razorpay_order_id,
        razorpay_signature=body.razorpay_signature,
    )


@router.post("/refund")
async def refund_payment(
    body: RefundIn,
    user: UserContext = Depends(require_permission("payments.refund")),
):
    return await _svc.initiate_refund(
        user=user,
        payment_id=body.payment_id,
        amount=body.amount,
        reason=body.reason,
    )
