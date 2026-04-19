"""Payment endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.payment_service import PaymentService
from app.services.elevenlabs_service import ElevenLabsService
from app.services.activity_log_service import log_activity

from app.core.database import get_connection
from app.core.logging import get_logger

router = APIRouter(prefix="/payments", tags=["Payments"])
_svc = PaymentService()
_voice_svc = ElevenLabsService()
logger = get_logger(__name__)


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


class RecordPaymentIn(BaseModel):
    order_id: str
    method: str = "cash"  # cash | upi | card | wallet | online
    amount: Optional[float] = None


class PaymentVoiceIn(BaseModel):
    amount: float
    language: str = "en"


@router.get("")
async def list_payments(
    order_by: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("payment.create")),
):
    """List payments for the current user's restaurant."""
    try:
        owner_id = user.owner_id if user.is_branch_user else user.user_id
        order_col = "created_at"
        order_dir = "DESC"
        if order_by:
            parts = order_by.split(":")
            allowed = {"created_at", "amount", "status"}
            if parts[0] in allowed:
                order_col = parts[0]
            if len(parts) > 1 and parts[1].lower() == "asc":
                order_dir = "ASC"
        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""
                SELECT p.* FROM payments p
                JOIN orders o ON o.id = p.order_id
                WHERE o.user_id = $1
                ORDER BY p.{order_col} {order_dir}
                LIMIT $2 OFFSET $3
                """,
                owner_id, limit, offset,
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("list_payments_failed", error=str(e), user_id=user.user_id)
        return []


@router.post("")
async def record_payment(
    body: RecordPaymentIn,
    user: UserContext = Depends(require_permission("payment.create")),
):
    """Record a payment for an order (called from POS save-and-print)."""
    return await _svc.initiate_payment(
        user=user,
        order_id=body.order_id,
        method=body.method,
        amount=body.amount,
    )


@router.post("/voice", response_class=Response)
async def payment_voice(
    body: PaymentVoiceIn,
    user: UserContext = Depends(require_permission("voice.use")),
):
    """Play a voice notification for a payment amount."""
    audio = await _voice_svc.payment_voice_notification(
        amount=body.amount,
        language=body.language,
    )
    return Response(content=audio, media_type="audio/mpeg")


@router.post("/initiate")
async def initiate_payment(
    body: InitiatePaymentIn,
    user: UserContext = Depends(require_permission("payment.create")),
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
    user: UserContext = Depends(require_permission("payment.create")),
):
    return await _svc.verify_razorpay_payment(
        razorpay_payment_id=body.razorpay_payment_id,
        razorpay_order_id=body.razorpay_order_id,
        razorpay_signature=body.razorpay_signature,
    )


@router.post("/refund")
async def refund_payment(
    body: RefundIn,
    user: UserContext = Depends(require_permission("payment.refund")),
):
    result = await _svc.initiate_refund(
        user=user,
        payment_id=body.payment_id,
        amount=body.amount,
        reason=body.reason,
    )
    await log_activity(
        user_id=user.user_id,
        branch_id=user.branch_id,
        action="payment.refund_issued",
        entity_type="payment",
        entity_id=body.payment_id,
        metadata={"amount": body.amount, "reason": body.reason},
    )
    return result
