"""
ERP Event Handlers — wires POS events to ERP side-effects.

ORDER_CONFIRMED  → Inventory deduction (recipe-based) + Inventory Ledger + COGS
ORDER_CANCELLED  → Inventory restoration + Ledger reversal
PAYMENT_COMPLETED → Accounting entry (revenue) + Double-entry journal + GST
PAYMENT_REFUNDED  → Accounting entry (refund) + Reverse journal
GRN_VERIFIED     → Inventory Ledger (purchase) + Journal (DR Inventory, CR A/P)
VENDOR_PAYMENT_MADE → Journal (DR A/P, CR Cash/Bank)
SHIFT_CLOSED     → Daily P&L aggregation

All handlers:
 - Respect feature flags (erp.auto_*)
 - Log to erp_event_log for audit
 - Handle platform tax config (skip GST when platform handles it)

PERFORMANCE: All handlers run ASYNC via the event bus.
Orders & payments are never blocked by ERP writes.

Registered at startup via register_erp_handlers().
"""
import time as _time

from app.core.events import (
    subscribe,
    DomainEvent,
    ORDER_CONFIRMED,
    ORDER_CANCELLED,
    PAYMENT_COMPLETED,
    PAYMENT_REFUNDED,
    GRN_VERIFIED,
    JOURNAL_ENTRY_CREATED,
    VENDOR_PAYMENT_MADE,
    SHIFT_CLOSED,
)
from app.core.database import get_connection
from app.core.logging import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# HELPERS — Feature flags, event logging, CoA lookup, journal creation
# ═══════════════════════════════════════════════════════════════════════

async def _is_flag_enabled(conn, restaurant_id: str, flag_name: str) -> bool:
    """Check if an ERP feature flag is enabled. Defaults to True if no row."""
    if not restaurant_id:
        return True
    row = await conn.fetchrow(
        "SELECT is_enabled FROM feature_flags WHERE restaurant_id = $1 AND flag_name = $2",
        restaurant_id, flag_name,
    )
    return row["is_enabled"] if row else True


async def _log_event(
    conn, event: DomainEvent, status: str = "completed",
    error_msg: str | None = None, elapsed_ms: int = 0,
):
    """Write to erp_event_log for audit trail."""
    try:
        await conn.execute(
            """INSERT INTO erp_event_log
                   (restaurant_id, event_type, reference_type, reference_id,
                    status, payload, error_message, processing_time_ms)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)""",
            event.restaurant_id,
            event.event_type,
            event.payload.get("reference_type"),
            event.payload.get("order_id") or event.payload.get("grn_id")
            or event.payload.get("payment_id"),
            status,
            None,  # skip full payload to save space
            error_msg,
            elapsed_ms,
        )
    except Exception:
        pass  # never let logging break the handler


async def _check_platform_tax(conn, restaurant_id: str, platform: str | None) -> bool:
    """Returns True if the platform handles GST externally."""
    if not platform or platform == "direct":
        return False
    row = await conn.fetchrow(
        """SELECT gst_handled_by_platform FROM platform_tax_config
           WHERE restaurant_id = $1 AND platform = $2 AND is_active = true""",
        restaurant_id, platform,
    )
    return row["gst_handled_by_platform"] if row else False

async def _get_account_id(conn, restaurant_id: str, account_code: str):
    """Look up a chart_of_accounts id by code. Returns None if not seeded."""
    row = await conn.fetchrow(
        "SELECT id FROM chart_of_accounts WHERE restaurant_id = $1 AND account_code = $2",
        restaurant_id, account_code,
    )
    return row["id"] if row else None


async def _create_journal_entry(
    conn,
    restaurant_id: str,
    branch_id: str | None,
    ref_type: str,
    ref_id: str,
    description: str,
    created_by: str,
    lines: list[dict],
):
    """
    Insert a balanced journal entry + lines.
    Each line: {"account_code": "1001", "debit": 100.0, "credit": 0.0}
    Resolves account_code → account_id automatically.
    Returns journal_entry_id or None on failure.
    """
    import json as _json

    # Resolve account codes to IDs
    resolved_lines = []
    for line in lines:
        account_id = await _get_account_id(conn, restaurant_id, line["account_code"])
        if not account_id:
            logger.warning(
                "journal_account_not_found",
                restaurant_id=restaurant_id,
                account_code=line["account_code"],
            )
            return None
        resolved_lines.append({
            "account_id": str(account_id),
            "debit": line.get("debit", 0),
            "credit": line.get("credit", 0),
            "description": line.get("description", ""),
        })

    try:
        entry_id = await conn.fetchval(
            "SELECT fn_create_journal_entry($1, $2, CURRENT_DATE, $3, $4, $5, $6, $7::jsonb)",
            restaurant_id,
            branch_id,
            ref_type,
            ref_id,
            description,
            created_by,
            _json.dumps(resolved_lines),
        )
        return entry_id
    except Exception:
        logger.exception(
            "journal_entry_creation_failed",
            ref_type=ref_type,
            ref_id=ref_id,
        )
        return None


# ═══════════════════════════════════════════════════════════════════════
# HANDLER: ORDER CONFIRMED
# 1. Deduct inventory (existing InventoryService)
# 2. Write to inventory_ledger (new append-only ledger)
# 3. Calculate & store COGS (new)
# 4. Journal: DR COGS, CR Inventory
# ═══════════════════════════════════════════════════════════════════════

async def _handle_order_confirmed(event: DomainEvent):
    """Deduct ingredient stock, record COGS, create GST invoice when an order is confirmed."""
    from app.services.inventory_service import InventoryService

    order_id = event.payload.get("order_id")
    if not order_id:
        return

    t0 = _time.monotonic()
    try:
        async with get_connection() as conn:
            # Feature flag check
            inv_enabled = await _is_flag_enabled(conn, event.restaurant_id, "erp.auto_inventory_deduction")
            gst_enabled = await _is_flag_enabled(conn, event.restaurant_id, "erp.auto_gst_invoice")

            items = await conn.fetch(
                "SELECT item_id, quantity FROM order_items WHERE order_id = $1",
                order_id,
            )
            if not items:
                return

        # 1. Existing inventory deduction (writes to ingredients + inventory_transactions)
        if inv_enabled:
            order_items = [{"item_id": r["item_id"], "quantity": r["quantity"]} for r in items]
            svc = InventoryService()
            await svc.deduct_for_order(
                user_id=event.user_id or "",
                order_id=order_id,
                order_items=order_items,
            )
            logger.info("erp_inventory_deducted", order_id=order_id)

            # 2 + 3. Inventory ledger + COGS (new ERP layer)
            await _write_ledger_and_cogs(event, order_id, order_items)

        # 4. GST Invoice creation (immutable snapshot)
        if gst_enabled and event.restaurant_id:
            await _create_order_gst_invoice(event, order_id)

        elapsed = int((_time.monotonic() - t0) * 1000)
        async with get_connection() as conn:
            await _log_event(conn, event, "completed", elapsed_ms=elapsed)

    except Exception:
        logger.exception("erp_order_confirmed_failed", order_id=order_id)
        elapsed = int((_time.monotonic() - t0) * 1000)
        try:
            async with get_connection() as conn:
                await _log_event(conn, event, "failed", str(order_id), elapsed)
        except Exception:
            pass


async def _create_order_gst_invoice(event: DomainEvent, order_id: str):
    """Create GST invoice using fn_create_gst_invoice. Skip if platform handles GST."""
    restaurant_id = event.restaurant_id
    branch_id = event.branch_id

    try:
        async with get_connection() as conn:
            # Check if platform handles GST externally
            order = await conn.fetchrow(
                "SELECT platform, is_interstate FROM orders WHERE id = $1", order_id,
            )
            if not order:
                return

            platform = order.get("platform") or "direct"
            is_interstate = order.get("is_interstate") or False
            gst_external = await _check_platform_tax(conn, restaurant_id, platform)

            if gst_external:
                # Mark order as externally handled
                await conn.execute(
                    "UPDATE orders SET gst_handled_externally = true WHERE id = $1",
                    order_id,
                )
                logger.info("erp_gst_external", order_id=order_id, platform=platform)
                return

            # Get restaurant GSTIN
            rest = await conn.fetchrow(
                "SELECT gst_number, state FROM restaurants WHERE id = $1",
                restaurant_id,
            )
            gstin = rest["gst_number"] if rest else None

            # Create the immutable GST invoice
            invoice_id = await conn.fetchval(
                "SELECT fn_create_gst_invoice($1, $2, $3, $4, NULL, $5, $6)",
                order_id, restaurant_id, branch_id,
                gstin, rest["state"] if rest else None, is_interstate,
            )

            logger.info("erp_gst_invoice_created", order_id=order_id, invoice_id=invoice_id)

    except Exception:
        logger.exception("erp_gst_invoice_failed", order_id=order_id)


async def _write_ledger_and_cogs(event: DomainEvent, order_id: str, order_items: list[dict]):
    """Write consumption entries to inventory_ledger and compute COGS."""
    restaurant_id = event.restaurant_id
    branch_id = event.branch_id
    user_id = event.user_id or ""
    total_cogs = 0.0

    try:
        async with get_connection() as conn:
            for oi in order_items:
                item_id = oi.get("item_id")
                quantity = oi.get("quantity", 1)

                # Try recipes first, fallback to item_ingredients
                mappings = await conn.fetch(
                    """
                    SELECT ri.ingredient_id, ri.quantity_required, ri.waste_percent,
                           i.cost_per_unit, i.name AS ingredient_name
                    FROM recipe_ingredients ri
                    JOIN recipes r ON r.id = ri.recipe_id AND r.is_active = true
                    JOIN ingredients i ON i.id = ri.ingredient_id
                    WHERE r.item_id = $1
                    """,
                    item_id,
                )

                if not mappings:
                    # Fallback to legacy item_ingredients
                    mappings = await conn.fetch(
                        """
                        SELECT ii.ingredient_id, ii.quantity_used AS quantity_required,
                               0::NUMERIC AS waste_percent,
                               i.cost_per_unit, i.name AS ingredient_name
                        FROM item_ingredients ii
                        JOIN ingredients i ON i.id = ii.ingredient_id
                        WHERE ii.item_id = $1
                        """,
                        item_id,
                    )

                for m in mappings:
                    qty_required = float(m["quantity_required"]) * quantity
                    waste = float(m["waste_percent"] or 0) / 100
                    qty_with_waste = qty_required * (1 + waste)
                    unit_cost = float(m["cost_per_unit"] or 0)
                    line_cost = qty_with_waste * unit_cost
                    total_cogs += line_cost

                    # Write to inventory_ledger
                    await conn.execute(
                        """
                        INSERT INTO inventory_ledger
                            (restaurant_id, branch_id, ingredient_id, transaction_type,
                             quantity_in, quantity_out, unit_cost,
                             reference_type, reference_id, notes, created_by)
                        VALUES ($1, $2, $3, 'consumption', 0, $4, $5,
                                'order', $6, $7, $8)
                        """,
                        restaurant_id,
                        branch_id,
                        str(m["ingredient_id"]),
                        qty_with_waste,
                        unit_cost,
                        order_id,
                        f"Order {order_id}: {m['ingredient_name']}",
                        user_id,
                    )

            # Update COGS on the order
            if total_cogs > 0:
                await conn.execute(
                    "UPDATE orders SET cost_of_goods_sold = $1 WHERE id = $2",
                    total_cogs, order_id,
                )

            # 4. Journal entry: DR COGS, CR Inventory (via accounting engine)
            if total_cogs > 0 and restaurant_id:
                from app.services.accounting_engine import accounting_engine
                await accounting_engine.record_cogs(
                    restaurant_id=restaurant_id,
                    branch_id=branch_id,
                    order_id=order_id,
                    amount=total_cogs,
                    created_by=user_id,
                )

        logger.info("erp_ledger_cogs_written", order_id=order_id, cogs=total_cogs)
    except Exception:
        logger.exception("erp_ledger_cogs_failed", order_id=order_id)


# ═══════════════════════════════════════════════════════════════════════
# HANDLER: ORDER CANCELLED
# 1. Restore inventory (existing)
# 2. Reverse inventory_ledger entries (new)
# 3. Reverse COGS journal entry (new)
# ═══════════════════════════════════════════════════════════════════════

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

        # Reverse ledger entries + COGS
        await _reverse_ledger_and_cogs(event, order_id)
    except Exception:
        logger.exception("erp_inventory_restore_failed", order_id=order_id)


async def _reverse_ledger_and_cogs(event: DomainEvent, order_id: str):
    """Reverse inventory_ledger consumption entries and COGS."""
    restaurant_id = event.restaurant_id
    branch_id = event.branch_id
    user_id = event.user_id or ""

    try:
        async with get_connection() as conn:
            # Find original consumption entries
            ledger_entries = await conn.fetch(
                """
                SELECT ingredient_id, quantity_out, unit_cost
                FROM inventory_ledger
                WHERE reference_type = 'order' AND reference_id = $1
                  AND transaction_type = 'consumption'
                """,
                order_id,
            )

            total_cogs_reversed = 0.0
            for entry in ledger_entries:
                qty = float(entry["quantity_out"])
                cost = float(entry["unit_cost"] or 0)
                total_cogs_reversed += qty * cost

                # Write return entry
                await conn.execute(
                    """
                    INSERT INTO inventory_ledger
                        (restaurant_id, branch_id, ingredient_id, transaction_type,
                         quantity_in, quantity_out, unit_cost,
                         reference_type, reference_id, notes, created_by)
                    VALUES ($1, $2, $3, 'return', $4, 0, $5,
                            'order', $6, 'Cancelled order restoration', $7)
                    """,
                    restaurant_id, branch_id,
                    str(entry["ingredient_id"]),
                    qty, cost, order_id, user_id,
                )

            # Reset COGS on order
            await conn.execute(
                "UPDATE orders SET cost_of_goods_sold = 0 WHERE id = $1", order_id,
            )

            # Reverse COGS journal via accounting engine
            if total_cogs_reversed > 0 and restaurant_id:
                from app.services.accounting_engine import accounting_engine
                # Find the original COGS journal entry and reverse it
                je_row = await conn.fetchrow(
                    "SELECT id FROM journal_entries WHERE restaurant_id = $1 "
                    "AND reference_type = 'inventory_consumption' AND reference_id = $2 "
                    "AND is_reversed = false LIMIT 1",
                    restaurant_id, f"cogs_{order_id}",
                )
                if je_row:
                    await accounting_engine.reverse_entry(
                        journal_entry_id=str(je_row["id"]),
                        reason=f"Order {order_id} cancelled",
                        created_by=user_id,
                    )
                else:
                    # Fallback: create explicit reversal journal
                    await accounting_engine.create_journal_entry(
                        reference_type="inventory_consumption",
                        reference_id=f"cogs_rev_{order_id}",
                        restaurant_id=restaurant_id,
                        branch_id=branch_id,
                        description=f"COGS reversal for cancelled order {order_id}",
                        created_by=user_id,
                        lines=[
                            {"account": "INVENTORY_FOOD", "debit": round(total_cogs_reversed, 2), "credit": 0,
                             "description": "Inventory restored"},
                            {"account": "COGS_FOOD", "debit": 0, "credit": round(total_cogs_reversed, 2),
                             "description": "COGS reversed"},
                        ],
                    )

        logger.info("erp_ledger_reversed", order_id=order_id, cogs_reversed=total_cogs_reversed)
    except Exception:
        logger.exception("erp_ledger_reversal_failed", order_id=order_id)


# ═══════════════════════════════════════════════════════════════════════
# HANDLER: PAYMENT COMPLETED
# 1. Single-entry accounting_entries (existing, backward compat)
# 2. Double-entry journal: DR Cash/Bank, CR Revenue, CR GST Payable
# 3. GST order_tax_details snapshot (new)
# ═══════════════════════════════════════════════════════════════════════

async def _handle_payment_completed(event: DomainEvent):
    """Record revenue via double-entry accounting engine on successful payment."""
    from app.services.accounting_engine import accounting_engine

    order_id = event.payload.get("order_id")
    amount = event.payload.get("amount", 0)
    method = event.payload.get("method", "unknown")
    payment_id = event.payload.get("payment_id")

    if not order_id or not amount:
        return

    t0 = _time.monotonic()
    try:
        # Single path: accounting engine handles idempotency internally.
        # No more dual single-entry + double-entry writes.
        journal_id = await accounting_engine.record_payment(
            restaurant_id=event.restaurant_id,
            branch_id=event.branch_id,
            payment_id=payment_id or order_id,
            order_id=order_id,
            amount=float(amount),
            method=method,
            created_by=event.user_id or "system",
        )

        if journal_id:
            logger.info("erp_payment_journal_created", order_id=order_id, amount=amount, journal_id=journal_id)
        else:
            logger.info("erp_payment_accounting_skipped", order_id=order_id, reason="zero_amount_or_missing_id")

        # GST journal (taxes) — only if taxes apply
        if event.restaurant_id:
            await _create_payment_gst_entries(event, order_id, float(amount), method)

        elapsed = int((_time.monotonic() - t0) * 1000)
        async with get_connection() as conn:
            await _log_event(conn, event, "completed", elapsed_ms=elapsed)

    except Exception:
        logger.exception("erp_revenue_record_failed", order_id=order_id)


async def _create_payment_gst_entries(
    event: DomainEvent, order_id: str, amount: float, method: str
):
    """
    Record GST liability entries for a payment, if the restaurant collects GST.
    This supplements the main payment journal (DR Cash, CR Receivable) by
    reclassifying part of revenue into tax payable accounts.
    """
    restaurant_id = event.restaurant_id
    if not restaurant_id:
        return

    try:
        async with get_connection() as conn:
            tax_details = await conn.fetch(
                "SELECT * FROM order_tax_details WHERE order_id = $1", order_id,
            )

            total_cgst = sum(float(t["cgst_amount"]) for t in tax_details) if tax_details else 0
            total_sgst = sum(float(t["sgst_amount"]) for t in tax_details) if tax_details else 0
            total_igst = sum(float(t["igst_amount"]) for t in tax_details) if tax_details else 0
            total_tax = total_cgst + total_sgst + total_igst

            if total_tax == 0:
                order = await conn.fetchrow(
                    "SELECT tax_amount FROM orders WHERE id = $1", order_id,
                )
                if order and order["tax_amount"]:
                    total_tax = float(order["tax_amount"])
                    total_cgst = round(total_tax / 2, 2)
                    total_sgst = total_tax - total_cgst

            if total_tax <= 0:
                return

            # Reclassify: DR Food Sales (reduce revenue by tax portion), CR GST Payable
            from app.services.accounting_engine import accounting_engine
            lines = [
                {"account": "FOOD_SALES", "debit": round(total_tax, 2), "credit": 0,
                 "description": "Revenue reclassified to GST payable"},
            ]
            if total_cgst > 0:
                lines.append({"account": "CGST_PAYABLE", "debit": 0, "credit": round(total_cgst, 2),
                               "description": "CGST collected"})
            if total_sgst > 0:
                lines.append({"account": "SGST_PAYABLE", "debit": 0, "credit": round(total_sgst, 2),
                               "description": "SGST collected"})
            if total_igst > 0:
                lines.append({"account": "IGST_PAYABLE", "debit": 0, "credit": round(total_igst, 2),
                               "description": "IGST collected"})

            await accounting_engine.create_journal_entry(
                reference_type="payment",
                reference_id=f"gst_{order_id}",
                restaurant_id=restaurant_id,
                branch_id=event.branch_id,
                description=f"GST reclassification for order {order_id}",
                created_by=event.user_id or "system",
                lines=lines,
            )

        logger.info("erp_gst_entries_created", order_id=order_id, total_tax=total_tax)
    except Exception:
        logger.exception("erp_gst_entries_failed", order_id=order_id)


# ═══════════════════════════════════════════════════════════════════════
# HANDLER: PAYMENT REFUNDED
# 1. Single-entry refund (existing)
# 2. Reverse journal: DR Revenue + GST Payable, CR Cash/Bank
# ═══════════════════════════════════════════════════════════════════════

async def _handle_payment_refunded(event: DomainEvent):
    """Record refund via accounting engine. Supports full and partial refunds."""
    from app.services.accounting_engine import accounting_engine

    order_id = event.payload.get("order_id")
    amount = event.payload.get("amount", 0)
    payment_id = event.payload.get("payment_id")
    method = event.payload.get("method", "cash")
    original_amount = event.payload.get("original_amount")
    is_partial = event.payload.get("is_partial", False)
    reason = event.payload.get("reason", "")

    if not order_id or not amount:
        return

    try:
        if is_partial and original_amount:
            # Partial refund path
            journal_id = await accounting_engine.record_partial_refund(
                restaurant_id=event.restaurant_id,
                branch_id=event.branch_id,
                payment_id=payment_id or order_id,
                order_id=order_id,
                refund_amount=float(amount),
                original_amount=float(original_amount),
                method=method,
                reason=reason,
                created_by=event.user_id or "system",
            )
            logger.info("erp_partial_refund_recorded", order_id=order_id,
                        amount=amount, original=original_amount, journal_id=journal_id)
        else:
            # Full refund path
            journal_id = await accounting_engine.record_refund(
                restaurant_id=event.restaurant_id,
                branch_id=event.branch_id,
                payment_id=payment_id or order_id,
                order_id=order_id,
                amount=float(amount),
                method=method,
                created_by=event.user_id or "system",
            )
            logger.info("erp_refund_recorded", order_id=order_id, amount=amount, journal_id=journal_id)

    except Exception:
        logger.exception("erp_refund_record_failed", order_id=order_id)


# ═══════════════════════════════════════════════════════════════════════
# HANDLER: GRN VERIFIED
# PO → GRN → Inventory Ledger → Journal (DR Inventory, CR A/P)
# ═══════════════════════════════════════════════════════════════════════

async def _handle_grn_verified(event: DomainEvent):
    """On GRN verification: write inventory_ledger + journal entry."""
    grn_id = event.payload.get("grn_id")
    restaurant_id = event.restaurant_id
    branch_id = event.branch_id
    user_id = event.user_id or ""

    if not grn_id or not restaurant_id:
        return

    try:
        async with get_connection() as conn:
            # Fetch GRN items
            grn_items = await conn.fetch(
                """
                SELECT gi.ingredient_id, gi.received_quantity, gi.unit_cost, gi.unit,
                       i.name AS ingredient_name
                FROM grn_items gi
                JOIN ingredients i ON i.id = gi.ingredient_id
                WHERE gi.grn_id = $1
                """,
                grn_id,
            )

            total_amount = 0.0
            for item in grn_items:
                qty = float(item["received_quantity"])
                cost = float(item["unit_cost"] or 0)
                line_total = qty * cost
                total_amount += line_total

                # Write to inventory_ledger (purchase IN)
                await conn.execute(
                    """
                    INSERT INTO inventory_ledger
                        (restaurant_id, branch_id, ingredient_id, transaction_type,
                         quantity_in, quantity_out, unit_cost,
                         reference_type, reference_id, notes, created_by)
                    VALUES ($1, $2, $3, 'purchase', $4, 0, $5,
                            'grn', $6, $7, $8)
                    """,
                    restaurant_id, branch_id,
                    str(item["ingredient_id"]),
                    qty, cost, str(grn_id),
                    f"GRN: {item['ingredient_name']} x {qty}",
                    user_id,
                )

                # Also update ingredients.current_stock for backward compat
                await conn.execute(
                    """
                    UPDATE ingredients
                    SET current_stock = current_stock + $1,
                        stock_quantity = stock_quantity + $1,
                        cost_per_unit = CASE WHEN $2 > 0 THEN $2 ELSE cost_per_unit END,
                        updated_at = NOW()
                    WHERE id = $3
                    """,
                    qty, cost, str(item["ingredient_id"]),
                )

            # Journal: DR Inventory (1004), CR Accounts Payable (2001)
            if total_amount > 0:
                from app.services.accounting_engine import accounting_engine
                await accounting_engine.record_grn(
                    restaurant_id=restaurant_id,
                    branch_id=branch_id,
                    grn_id=str(grn_id),
                    amount=total_amount,
                    created_by=user_id,
                )

        logger.info("erp_grn_processed", grn_id=grn_id, total=total_amount)
    except Exception:
        logger.exception("erp_grn_processing_failed", grn_id=grn_id)


# ═══════════════════════════════════════════════════════════════════════
# REGISTRATION
# ═══════════════════════════════════════════════════════════════════════

def register_erp_handlers():
    """Call once at startup to wire ERP event handlers."""
    subscribe(ORDER_CONFIRMED, _handle_order_confirmed)
    subscribe(ORDER_CANCELLED, _handle_order_cancelled)
    subscribe(PAYMENT_COMPLETED, _handle_payment_completed)
    subscribe(PAYMENT_REFUNDED, _handle_payment_refunded)
    subscribe(GRN_VERIFIED, _handle_grn_verified)
    subscribe(VENDOR_PAYMENT_MADE, _handle_vendor_payment)
    subscribe(SHIFT_CLOSED, _handle_shift_closed)
    logger.info("erp_event_handlers_registered")


# ═══════════════════════════════════════════════════════════════════════
# HANDLER: VENDOR PAYMENT
# Journal: DR Accounts Payable, CR Cash/Bank (via SQL function)
# ═══════════════════════════════════════════════════════════════════════

async def _handle_vendor_payment(event: DomainEvent):
    """Create journal entry for vendor payment via accounting engine."""
    from app.services.accounting_engine import accounting_engine

    payment_id = event.payload.get("payment_id")
    vendor_id = event.payload.get("vendor_id", "")
    amount = event.payload.get("amount", 0)
    method = event.payload.get("method", "cash")
    restaurant_id = event.restaurant_id

    if not payment_id or not restaurant_id or not amount:
        return

    t0 = _time.monotonic()
    try:
        async with get_connection() as conn:
            if not await _is_flag_enabled(conn, restaurant_id, "erp.auto_journal_entries"):
                return

        journal_id = await accounting_engine.record_vendor_payment(
            restaurant_id=restaurant_id,
            branch_id=event.branch_id,
            payment_id=payment_id,
            vendor_id=vendor_id,
            amount=float(amount),
            method=method,
            created_by=event.user_id or "system",
        )
        logger.info(
            "erp_vendor_payment_journal",
            payment_id=payment_id, journal_id=journal_id,
        )
        elapsed = int((_time.monotonic() - t0) * 1000)
        async with get_connection() as conn:
            await _log_event(conn, event, "completed", elapsed_ms=elapsed)
    except Exception:
        logger.exception("erp_vendor_payment_failed", payment_id=payment_id)


# ═══════════════════════════════════════════════════════════════════════
# HANDLER: SHIFT CLOSED
# Aggregate daily P&L for the shift's branch/date
# ═══════════════════════════════════════════════════════════════════════

async def _handle_shift_closed(event: DomainEvent):
    """Aggregate daily P&L + record cash drawer journal when a shift is closed."""
    from app.services.accounting_engine import accounting_engine

    restaurant_id = event.restaurant_id
    branch_id = event.branch_id

    if not restaurant_id or not branch_id:
        return

    t0 = _time.monotonic()
    try:
        async with get_connection() as conn:
            if not await _is_flag_enabled(conn, restaurant_id, "erp.daily_pnl_auto_aggregate"):
                return

            pnl_id = await conn.fetchval(
                "SELECT fn_aggregate_daily_pnl_v2($1, $2, CURRENT_DATE)",
                restaurant_id, branch_id,
            )
            logger.info(
                "erp_daily_pnl_aggregated",
                restaurant_id=restaurant_id, pnl_id=pnl_id,
            )

            # Cash drawer → ledger: sum payments by method for this shift
            shift_id = event.payload.get("shift_id") or event.payload.get("drawer_id")
            if shift_id:
                payment_totals = await conn.fetchrow(
                    """
                    SELECT
                        COALESCE(SUM(amount) FILTER (WHERE payment_method IN ('cash', 'CASH')), 0) AS cash_total,
                        COALESCE(SUM(amount) FILTER (WHERE payment_method IN ('card', 'CARD', 'credit_card', 'debit_card')), 0) AS card_total,
                        COALESCE(SUM(amount) FILTER (WHERE payment_method IN ('upi', 'UPI', 'bank', 'neft', 'rtgs')), 0) AS upi_total
                    FROM payments
                    WHERE restaurant_id = $1
                      AND branch_id = $2
                      AND created_at::date = CURRENT_DATE
                      AND status = 'completed'
                    """,
                    restaurant_id, branch_id,
                )

                if payment_totals:
                    await accounting_engine.record_shift_close(
                        restaurant_id=restaurant_id,
                        branch_id=branch_id,
                        shift_id=str(shift_id),
                        cash_sales=float(payment_totals["cash_total"]),
                        card_sales=float(payment_totals["card_total"]),
                        upi_sales=float(payment_totals["upi_total"]),
                        created_by=event.user_id or "system",
                    )
                    logger.info("erp_shift_cash_drawer_journal", shift_id=shift_id)

            elapsed = int((_time.monotonic() - t0) * 1000)
            await _log_event(conn, event, "completed", elapsed_ms=elapsed)
    except Exception:
        logger.exception("erp_shift_close_pnl_failed", restaurant_id=restaurant_id)
