"""Webhook endpoints — Razorpay payment & subscription callbacks."""
import hmac
import hashlib
from fastapi import APIRouter, Request, HTTPException

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.payment_service import PaymentService
from app.services.subscription_service import SubscriptionService

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])
logger = get_logger(__name__)

_payment_svc = PaymentService()
_subscription_svc = SubscriptionService()


def _verify_razorpay_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.HMAC(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/razorpay/payment")
async def razorpay_payment_webhook(request: Request):
    """Razorpay payment event callback."""
    settings = get_settings()
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not _verify_razorpay_signature(body, signature, settings.RAZORPAY_WEBHOOK_SECRET):
        raise HTTPException(status_code=400, detail="Invalid signature")

    payload = await request.json()
    event = payload.get("event", "")
    entity = payload.get("payload", {}).get("payment", {}).get("entity", {})

    logger.info("razorpay_payment_webhook", event=event, payment_id=entity.get("id"))
    await _payment_svc.handle_webhook(event=event, payload=entity)
    return {"status": "ok"}


@router.post("/razorpay/subscription")
async def razorpay_subscription_webhook(request: Request):
    """Razorpay subscription lifecycle callback."""
    settings = get_settings()
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not _verify_razorpay_signature(body, signature, settings.RAZORPAY_WEBHOOK_SECRET):
        raise HTTPException(status_code=400, detail="Invalid signature")

    payload = await request.json()
    event = payload.get("event", "")

    logger.info("razorpay_subscription_webhook", event=event)
    await _subscription_svc.handle_payment_webhook(event_type=event, payload=payload)
    return {"status": "ok"}
