"""Inventory → Accounting bridge.

Section 5 — Accounting Integration.

Subscribes to inventory domain events and posts the matching double-entry
journal via the accounting engine. Wired in at app startup alongside the
ERP handlers.

Posting rules
─────────────
INVENTORY_WASTED         DR Wastage Expense  / CR Inventory Food
INVENTORY_PURCHASED      DR Inventory Food   / CR Accounts Payable
INVENTORY_RETURN_TO_VENDOR  DR Accounts Payable / CR Inventory Food
INVENTORY_EXPIRED        DR Wastage Expense  / CR Inventory Food

Other event types (CONSUMED, ADJUSTED, RECOUNTED, TRANSFERRED_*) do not
generate journal entries here:
  • CONSUMED is already handled by `_handle_order_confirmed` (COGS journal).
  • ADJUSTED / RECOUNTED have no P&L impact in this MVP (treated as
    operational corrections; can be revisited in Phase 2).
  • TRANSFERRED_* moves stock between branches owned by the same legal
    entity — no GL posting required.

All journal entries are idempotent: reference_id encodes the originating
ledger event_id, so re-firing produces zero duplicates (handled by
accounting_engine's natural-key dedup).
"""
from __future__ import annotations

from app.core.events import (
    DomainEvent, subscribe,
    INVENTORY_WASTED, INVENTORY_PURCHASED,
    INVENTORY_RETURN_TO_VENDOR, INVENTORY_EXPIRED,
)
from app.core.logging import get_logger

logger = get_logger(__name__)


def _value(event: DomainEvent) -> float:
    """Return absolute monetary value of the inventory movement."""
    p = event.payload or {}
    qty = float(p.get("quantity_in", 0) or 0) + float(p.get("quantity_out", 0) or 0)
    cost = float(p.get("unit_cost", 0) or 0)
    return abs(qty * cost)


async def _post_journal(event: DomainEvent, *,
                        debit_account: str, credit_account: str,
                        reference_type: str, description: str):
    from app.services.accounting_engine import accounting_engine

    amt = _value(event)
    if amt <= 0 or not event.restaurant_id:
        return

    event_id = (event.payload or {}).get("event_id")
    ref_id = str(event_id) if event_id else f"{reference_type}:{event.correlation_id or event.timestamp}"

    try:
        await accounting_engine.create_journal_entry(
            reference_type=reference_type,
            reference_id=ref_id,
            restaurant_id=event.restaurant_id,
            branch_id=event.branch_id,
            description=description,
            created_by=event.user_id or "system",
            lines=[
                {"account": debit_account,  "debit": round(amt, 2), "credit": 0,
                 "description": description},
                {"account": credit_account, "debit": 0, "credit": round(amt, 2),
                 "description": description},
            ],
        )
    except Exception:
        logger.exception(
            "inventory_journal_failed",
            event_type=event.event_type, ref_id=ref_id, amt=amt,
        )


async def _handle_wasted(event: DomainEvent):
    await _post_journal(
        event,
        debit_account="WASTAGE_EXPENSE",
        credit_account="INVENTORY_FOOD",
        reference_type="wastage",
        description="Inventory wastage",
    )


async def _handle_expired(event: DomainEvent):
    await _post_journal(
        event,
        debit_account="WASTAGE_EXPENSE",
        credit_account="INVENTORY_FOOD",
        reference_type="wastage",
        description="Inventory expired (write-off)",
    )


async def _handle_purchased(event: DomainEvent):
    await _post_journal(
        event,
        debit_account="INVENTORY_FOOD",
        credit_account="ACCOUNTS_PAYABLE",
        reference_type="inventory_purchase",
        description="Inventory purchased (event)",
    )


async def _handle_return_to_vendor(event: DomainEvent):
    await _post_journal(
        event,
        debit_account="ACCOUNTS_PAYABLE",
        credit_account="INVENTORY_FOOD",
        reference_type="inventory_purchase",
        description="Return to vendor",
    )


def register_inventory_accounting_handlers():
    """Wire inventory → accounting subscribers. Called once at startup."""
    subscribe(INVENTORY_WASTED, _handle_wasted)
    subscribe(INVENTORY_EXPIRED, _handle_expired)
    subscribe(INVENTORY_PURCHASED, _handle_purchased)
    subscribe(INVENTORY_RETURN_TO_VENDOR, _handle_return_to_vendor)
    logger.info("inventory_accounting_handlers_registered")
