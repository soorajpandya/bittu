"""Webhook endpoints — gateway-agnostic, replay-safe, forensic-stored.

NOTE: Razorpay-specific code paths are being removed in subsequent batches.
For now we keep the route names but the verification + storage goes through
`app.core.webhook_security` so all gateways share one safe pipeline.
"""
from fastapi import APIRouter, Request

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.webhook_security import verify_and_register_webhook
from app.services.payment_service import PaymentService

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])
logger = get_logger(__name__)

_payment_svc = PaymentService()


def _razorpay_payment_event_id(payload: dict) -> str | None:
    return (
        payload.get("payload", {})
        .get("payment", {})
        .get("entity", {})
        .get("id")
    )


@router.post("/razorpay/payment")
async def razorpay_payment_webhook(request: Request):
    """Razorpay payment event callback (replay-safe)."""
    settings = get_settings()
    body = await request.body()

    result = await verify_and_register_webhook(
        request=request,
        body=body,
        gateway="razorpay",
        secret=settings.RAZORPAY_WEBHOOK_SECRET,
        event_id_extractor=_razorpay_payment_event_id,
        event_type_extractor=lambda p: p.get("event"),
    )
    if result.duplicate:
        return {"status": "ok", "duplicate": True, "event_id": result.event_id}

    payload = await request.json()
    event = payload.get("event", "")
    entity = payload.get("payload", {}).get("payment", {}).get("entity", {})

    logger.info(
        "razorpay_payment_webhook",
        event=event,
        payment_id=entity.get("id"),
        latency_ms=result.latency_ms,
    )
    try:
        await _payment_svc.handle_webhook(
            event=event,
            payload=entity,
            raw_payload=payload,
            signature=request.headers.get("X-Razorpay-Signature", ""),
            gateway="razorpay",
        )
        await result.mark_processed(status="processed")
    except Exception as exc:
        await result.mark_processed(status="failed", error=str(exc)[:500])
        raise
    return {"status": "ok"}


