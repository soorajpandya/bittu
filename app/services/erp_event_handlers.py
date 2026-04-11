"""
ERP Event Handlers — wires POS events to ERP side-effects.

ORDER_CONFIRMED  → Inventory deduction (recipe-based)
ORDER_CANCELLED  → Inventory restoration
PAYMENT_COMPLETED → Accounting entry (revenue)
PAYMENT_REFUNDED  → Accounting entry (refund)

Registered at startup via register_erp_handlers().
"""
from app.core.events import (
    subscribe,
    DomainEvent,
    ORDER_CONFIRMED,
    ORDER_CANCELLED,
    PAYMENT_COMPLETED,
    PAYMENT_REFUNDED,
)
from app.core.database import get_connection
from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Inventory deduction on order confirmation ──

async def _handle_order_confirmed(event: DomainEvent):
    """Deduct ingredient stock when an order is confirmed."""
    from app.services.inventory_service import InventoryService

    order_id = event.payload.get("order_id")
    if not order_id:
        return

    try:
        async with get_connection() as conn:
            items = await conn.fetch(
                "SELECT item_id, quantity FROM order_items WHERE order_id = $1",
                order_id,
            )
            if not items:
                return

        order_items = [{"item_id": r["item_id"], "quantity": r["quantity"]} for r in items]
        svc = InventoryService()
        await svc.deduct_for_order(
            user_id=event.user_id or "",
            order_id=order_id,
            order_items=order_items,
        )
        logger.info("erp_inventory_deducted", order_id=order_id)
    except Exception:
        logger.exception("erp_inventory_deduction_failed", order_id=order_id)


# ── Inventory restoration on order cancellation ──

async def _handle_order_cancelled(event: DomainEvent):
    """Restore ingredient stock when an order is cancelled."""
    from app.services.inventory_service import InventoryService

    order_id = event.payload.get("order_id")
    if not order_id:
        return

    try:
        svc = InventoryService()
        await svc.restore_for_order(
            user_id=event.user_id or "",
            order_id=order_id,
        )
        logger.info("erp_inventory_restored", order_id=order_id)
    except Exception:
        logger.exception("erp_inventory_restore_failed", order_id=order_id)


# ── Accounting entry on payment completed ──

async def _handle_payment_completed(event: DomainEvent):
    """Insert revenue entry into accounting_entries on successful payment."""
    from app.services.accounting_service import AccountingService

    order_id = event.payload.get("order_id")
    amount = event.payload.get("amount", 0)
    method = event.payload.get("method", "unknown")
    payment_id = event.payload.get("payment_id")

    if not order_id or not amount:
        return

    try:
        svc = AccountingService()
        await svc.record_revenue(
            user_id=event.user_id,
            restaurant_id=event.restaurant_id,
            branch_id=event.branch_id,
            order_id=order_id,
            payment_id=payment_id,
            amount=float(amount),
            method=method,
        )
        logger.info("erp_revenue_recorded", order_id=order_id, amount=amount)
    except Exception:
        logger.exception("erp_revenue_record_failed", order_id=order_id)


# ── Accounting entry on refund ──

async def _handle_payment_refunded(event: DomainEvent):
    """Insert refund (negative revenue) entry into accounting_entries."""
    from app.services.accounting_service import AccountingService

    order_id = event.payload.get("order_id")
    amount = event.payload.get("amount", 0)
    payment_id = event.payload.get("payment_id")

    if not order_id or not amount:
        return

    try:
        svc = AccountingService()
        await svc.record_refund(
            user_id=event.user_id,
            restaurant_id=event.restaurant_id,
            branch_id=event.branch_id,
            order_id=order_id,
            payment_id=payment_id,
            amount=float(amount),
        )
        logger.info("erp_refund_recorded", order_id=order_id, amount=amount)
    except Exception:
        logger.exception("erp_refund_record_failed", order_id=order_id)


# ── Registration ──

def register_erp_handlers():
    """Call once at startup to wire ERP event handlers."""
    subscribe(ORDER_CONFIRMED, _handle_order_confirmed)
    subscribe(ORDER_CANCELLED, _handle_order_cancelled)
    subscribe(PAYMENT_COMPLETED, _handle_payment_completed)
    subscribe(PAYMENT_REFUNDED, _handle_payment_refunded)
    logger.info("erp_event_handlers_registered")
