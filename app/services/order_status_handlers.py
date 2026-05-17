"""
Order ↔ payment status sync.

When a payment intent dies (operator cancel, abandoned QR auto-expire,
gateway-side failure), the parent order must also leave the
"awaiting payment" world so it stops showing up in operator queues and
stops inflating revenue / order-count / AOV in reports.

We do this via in-process event subscribers so the producer side
(`payment_intents.cancel_intent`, the Razorpay webhook dispatcher, etc.)
only has to emit `PAYMENT_CANCELLED` / `PAYMENT_EXPIRED` and the order
flip happens for free.

Guarantees:
- Idempotent — the UPDATE is guarded by a status whitelist, so replays
  or duplicate webhooks are no-ops.
- Safe — never overwrites `completed`, `paid`, `captured`, `refunded`,
  or an already-`cancelled` order. The latter is critical because
  `payment.cancelled` already runs an inline UPDATE in
  `payment_intents.cancel_intent`; this subscriber is a backstop.
- Emits a follow-up `ORDER_CANCELLED` only when this subscriber was the
  one that flipped the row, so realtime listeners aren't double-fired.
"""
from __future__ import annotations

from app.core.database import get_service_connection
from app.core.events import (
    DomainEvent,
    PAYMENT_CANCELLED,
    PAYMENT_EXPIRED,
    ORDER_CANCELLED,
    emit_and_publish,
    subscribe,
)
from app.core.logging import get_logger

logger = get_logger(__name__)


async def _flip_order_to_cancelled(event: DomainEvent) -> None:
    """Flip orders.status → 'cancelled' for an order whose payment died."""
    payload = event.payload or {}
    order_id = payload.get("order_id")
    if not order_id:
        return

    try:
        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                """
                UPDATE orders
                   SET status = 'cancelled',
                       updated_at = NOW()
                 WHERE id = $1::uuid
                   AND LOWER(status) NOT IN (
                        'cancelled','canceled','refunded',
                        'completed','paid','captured'
                   )
                RETURNING id::text AS order_id,
                          restaurant_id::text AS restaurant_id,
                          branch_id::text AS branch_id
                """,
                order_id,
            )
    except Exception:
        # DB hiccup must never break the payment-event chain. Reports stay
        # slightly off until reconciliation; far better than dropping the
        # whole event pipeline.
        logger.exception("order_cancel_on_payment_death_failed", order_id=order_id)
        return

    if row is None:
        # Order was already in a terminal status — nothing to do.
        return

    try:
        source = (
            "payment_expired"
            if event.event_type == PAYMENT_EXPIRED
            else "payment_cancelled"
        )
        await emit_and_publish(DomainEvent(
            event_type=ORDER_CANCELLED,
            payload={
                "order_id": row["order_id"],
                "reason": payload.get("reason") or source,
                "source": source,
                "payment_id": payload.get("payment_id"),
            },
            restaurant_id=row["restaurant_id"],
            branch_id=row["branch_id"],
            user_id=event.user_id,
            correlation_id=event.correlation_id,
        ))
    except Exception:
        logger.exception(
            "order_cancelled_emit_failed",
            order_id=row["order_id"],
            source=event.event_type,
        )

    logger.info(
        "order_auto_cancelled_on_payment_death",
        order_id=row["order_id"],
        source=event.event_type,
    )


def register_order_status_handlers() -> None:
    """Wire payment-death events to the order-cancel subscriber. Idempotent."""
    subscribe(PAYMENT_CANCELLED, _flip_order_to_cancelled)
    subscribe(PAYMENT_EXPIRED, _flip_order_to_cancelled)
