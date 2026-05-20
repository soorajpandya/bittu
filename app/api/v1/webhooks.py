"""Webhook endpoints — gateway-agnostic, replay-safe, forensic-stored.

The Razorpay route delegates to `app.services.razorpay.webhook_dispatcher`,
which routes per-event to handlers that update `rzp_*` mirror tables, the
canonical `payments` / `orders` rows, and (for `payment.captured`) the
merchant ledger + escrow hold.
"""
import hashlib

from fastapi import APIRouter, Request

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.webhook_security import verify_and_register_webhook
from app.services.payment_service import PaymentService
from app.services.razorpay.webhook_dispatcher import dispatch_event as rzp_dispatch_event

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])
logger = get_logger(__name__)

_payment_svc = PaymentService()  # retained for non-razorpay gateways


def _make_razorpay_event_id_extractor(header_event_id: str | None):
    """
    Build a closure that prefers the per-request `X-Razorpay-Event-Id` header
    (Razorpay's true unique event id), and falls back to a stable hash of
    event-type + entity-id for older payloads. The previous extractor used
    only `payment.entity.id`, which collides across event types for the
    same payment (e.g. authorized + captured) and incorrectly trips the
    transport-level dedupe.
    """
    def _extract(payload: dict) -> str | None:
        if header_event_id:
            return header_event_id
        event = payload.get("event") or ""
        # Try the most distinguishing entity id available per event family.
        body = payload.get("payload") or {}
        for key in ("payment", "refund", "dispute", "settlement",
                    "order", "qr_code", "invoice", "virtual_account"):
            ent = (body.get(key) or {}).get("entity") or {}
            if ent.get("id"):
                seed = f"{event}|{key}|{ent['id']}|{payload.get('created_at') or ''}"
                return f"rzp_synth_{hashlib.sha256(seed.encode()).hexdigest()[:32]}"
        return None
    return _extract


@router.post("/razorpay/payment")
async def razorpay_payment_webhook(request: Request):
    """Razorpay event callback — verified, deduped, dispatched to per-event handler."""
    settings = get_settings()
    body = await request.body()

    header_event_id = request.headers.get("X-Razorpay-Event-Id")
    signature = request.headers.get("X-Razorpay-Signature", "")

    result = await verify_and_register_webhook(
        request=request,
        body=body,
        gateway="razorpay",
        secret=settings.RAZORPAY_WEBHOOK_SECRET,
        event_id_extractor=_make_razorpay_event_id_extractor(header_event_id),
        event_type_extractor=lambda p: p.get("event"),
    )
    if result.duplicate:
        return {"status": "ok", "duplicate": True, "event_id": result.event_id}

    payload = await request.json()
    event = payload.get("event", "")

    logger.info(
        "razorpay_webhook_received",
        event_name=event,
        event_id=result.event_id,
        latency_ms=result.latency_ms,
    )
    try:
        outcome = await rzp_dispatch_event(
            event=event,
            envelope=payload,
            signature=signature,
        )
        await result.mark_processed(status="processed")
    except Exception as exc:
        await result.mark_processed(status="failed", error=str(exc)[:500])
        raise
    return {"status": "ok", "event": event, "result": outcome}


