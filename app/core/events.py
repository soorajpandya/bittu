"""
Domain event system for event-driven architecture.
Events are emitted synchronously in-process and optionally published to Redis pub/sub.
"""
import orjson
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine
from dataclasses import dataclass, field
from collections import defaultdict

from app.core.logging import get_logger

logger = get_logger(__name__)

# ── Event Types ──

# Order events
ORDER_CREATED = "order.created"
ORDER_CONFIRMED = "order.confirmed"
ORDER_STATUS_CHANGED = "order.status_changed"
ORDER_CANCELLED = "order.cancelled"

# Payment events
PAYMENT_INITIATED = "payment.initiated"
PAYMENT_COMPLETED = "payment.completed"
PAYMENT_FAILED = "payment.failed"
PAYMENT_REFUNDED = "payment.refunded"

# Kitchen events
KITCHEN_ORDER_CREATED = "kitchen.order_created"
KITCHEN_STATUS_CHANGED = "kitchen.status_changed"
KITCHEN_ITEM_READY = "kitchen.item_ready"

# Table events
TABLE_SESSION_STARTED = "table.session_started"
TABLE_SESSION_ENDED = "table.session_ended"
TABLE_CART_UPDATED = "table.cart_updated"
TABLE_ORDER_PLACED = "table.order_placed"
TABLE_STATUS_CHANGED = "table.status_changed"
TABLE_CALL_WAITER = "table.call_waiter"

# Inventory events
INVENTORY_DEDUCTED = "inventory.deducted"
INVENTORY_RESTORED = "inventory.restored"
INVENTORY_LOW_STOCK = "inventory.low_stock"

# Delivery events
DELIVERY_ASSIGNED = "delivery.assigned"
DELIVERY_STATUS_CHANGED = "delivery.status_changed"
DELIVERY_LOCATION_UPDATED = "delivery.location_updated"

# Subscription events
SUBSCRIPTION_ACTIVATED = "subscription.activated"
SUBSCRIPTION_PAYMENT_FAILED = "subscription.payment_failed"
SUBSCRIPTION_CANCELLED = "subscription.cancelled"

# Alert / notification events
ALERT_CREATED = "alert.created"
NOTIFICATION_SENT = "notification.sent"

# Accounting sync events
ACCOUNTING_INVOICE_CREATED = "accounting.invoice_created"
ACCOUNTING_INVOICE_VOIDED = "accounting.invoice_voided"
ACCOUNTING_CREDITNOTE_CREATED = "accounting.creditnote_created"
ACCOUNTING_SYNC_COMPLETED = "accounting.sync_completed"

# ── ERP events (006_erp_full_system) ──

# Double-entry accounting
JOURNAL_ENTRY_CREATED = "accounting.journal_entry_created"

# Procurement
PURCHASE_ORDER_APPROVED = "purchase.order_approved"
PURCHASE_RECEIVED = "purchase.received"
GRN_CREATED = "grn.created"
GRN_VERIFIED = "grn.verified"
VENDOR_PAYMENT_MADE = "vendor.payment_made"

# Stock transfers
STOCK_TRANSFER_SHIPPED = "stock.transfer_shipped"
STOCK_TRANSFER_RECEIVED = "stock.transfer_received"

# Cash & Shift management
SHIFT_OPENED = "shift.opened"
SHIFT_CLOSED = "shift.closed"

# GST / Invoicing
GST_INVOICE_CREATED = "gst.invoice_created"
GST_REPORT_GENERATED = "gst.report_generated"


@dataclass
class DomainEvent:
    """Immutable domain event with metadata."""
    event_type: str
    payload: dict[str, Any]
    user_id: str | None = None
    restaurant_id: str | None = None
    branch_id: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    correlation_id: str | None = None

    def to_json(self) -> str:
        return orjson.dumps({
            "event_type": self.event_type,
            "payload": self.payload,
            "user_id": self.user_id,
            "restaurant_id": self.restaurant_id,
            "branch_id": self.branch_id,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
        }).decode()


# ── Event Bus (in-process) ──

EventHandler = Callable[[DomainEvent], Coroutine[Any, Any, None]]

_handlers: dict[str, list[EventHandler]] = defaultdict(list)


def subscribe(event_type: str, handler: EventHandler):
    """Register a handler for a specific event type."""
    _handlers[event_type].append(handler)


def subscribe_pattern(pattern: str, handler: EventHandler):
    """Register a handler for events matching a prefix (e.g., 'order.*')."""
    _handlers[f"__pattern__{pattern}"].append(handler)


async def emit(event: DomainEvent):
    """
    Emit an event to all registered handlers.
    Handlers run sequentially — failures are logged but don't block the emitter.
    """
    # Direct handlers
    for handler in _handlers.get(event.event_type, []):
        try:
            await handler(event)
        except Exception:
            logger.exception(
                "event_handler_error",
                event_type=event.event_type,
                handler=handler.__name__,
            )

    # Pattern handlers
    for pattern_key, handlers in _handlers.items():
        if pattern_key.startswith("__pattern__"):
            pattern = pattern_key[len("__pattern__"):]
            if event.event_type.startswith(pattern.rstrip("*")):
                for handler in handlers:
                    try:
                        await handler(event)
                    except Exception:
                        logger.exception(
                            "pattern_handler_error",
                            event_type=event.event_type,
                            pattern=pattern,
                        )


async def emit_to_redis(event: DomainEvent):
    """Publish event to Redis pub/sub for cross-instance distribution."""
    from app.core.redis import get_redis
    try:
        r = get_redis()
        channel = f"events:{event.event_type}"
        await r.publish(channel, event.to_json())

        # Also publish to restaurant-specific channel for WebSocket fan-out
        if event.restaurant_id:
            restaurant_channel = f"restaurant:{event.restaurant_id}:events"
            await r.publish(restaurant_channel, event.to_json())
    except Exception:
        logger.exception("redis_publish_error", event_type=event.event_type)


async def emit_and_publish(event: DomainEvent):
    """Emit in-process AND publish to Redis."""
    await emit(event)
    await emit_to_redis(event)
