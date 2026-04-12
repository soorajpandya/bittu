"""
ERP Event Handlers — wires POS events to ERP side-effects.

ORDER_CONFIRMED  → Inventory deduction (recipe-based) + Inventory Ledger + COGS
ORDER_CANCELLED  → Inventory restoration + Ledger reversal
PAYMENT_COMPLETED → Accounting entry (revenue) + Double-entry journal + GST
PAYMENT_REFUNDED  → Accounting entry (refund) + Reverse journal
GRN_VERIFIED     → Inventory Ledger (purchase) + Journal (DR Inventory, CR A/P)

PERFORMANCE: All handlers run ASYNC via the event bus.
Orders & payments are never blocked by ERP writes.

Registered at startup via register_erp_handlers().
"""
from app.core.events import (
    subscribe,
    DomainEvent,
    ORDER_CONFIRMED,
    ORDER_CANCELLED,
    PAYMENT_COMPLETED,
    PAYMENT_REFUNDED,
    GRN_VERIFIED,
    JOURNAL_ENTRY_CREATED,
)
from app.core.database import get_connection
from app.core.logging import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# HELPERS — Chart of Accounts lookup + Journal Entry creation
# ═══════════════════════════════════════════════════════════════════════

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
    """Deduct ingredient stock and record COGS when an order is confirmed."""
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

        # 1. Existing inventory deduction (writes to ingredients + inventory_transactions)
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

    except Exception:
        logger.exception("erp_inventory_deduction_failed", order_id=order_id)


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

            # 4. Journal entry: DR COGS, CR Inventory
            if total_cogs > 0 and restaurant_id:
                await _create_journal_entry(
                    conn,
                    restaurant_id=restaurant_id,
                    branch_id=branch_id,
                    ref_type="order",
                    ref_id=order_id,
                    description=f"COGS for order {order_id}",
                    created_by=user_id,
                    lines=[
                        {"account_code": "5001", "debit": round(total_cogs, 2), "credit": 0,
                         "description": "Cost of goods sold"},
                        {"account_code": "1004", "debit": 0, "credit": round(total_cogs, 2),
                         "description": "Inventory consumed"},
                    ],
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

            # Reverse COGS journal: DR Inventory, CR COGS
            if total_cogs_reversed > 0 and restaurant_id:
                await _create_journal_entry(
                    conn,
                    restaurant_id=restaurant_id,
                    branch_id=branch_id,
                    ref_type="order_cancel",
                    ref_id=order_id,
                    description=f"COGS reversal for cancelled order {order_id}",
                    created_by=user_id,
                    lines=[
                        {"account_code": "1004", "debit": round(total_cogs_reversed, 2), "credit": 0,
                         "description": "Inventory restored"},
                        {"account_code": "5001", "debit": 0, "credit": round(total_cogs_reversed, 2),
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
    """Insert revenue entry + double-entry journal on successful payment."""
    from app.services.accounting_service import AccountingService

    order_id = event.payload.get("order_id")
    amount = event.payload.get("amount", 0)
    method = event.payload.get("method", "unknown")
    payment_id = event.payload.get("payment_id")

    if not order_id or not amount:
        return

    try:
        # 1. Backward-compat single-entry
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

        # 2 + 3. Double-entry journal + GST
        await _create_payment_journal(event, order_id, float(amount), method)

    except Exception:
        logger.exception("erp_revenue_record_failed", order_id=order_id)


async def _create_payment_journal(
    event: DomainEvent, order_id: str, amount: float, method: str
):
    """Create double-entry journal for payment with GST breakdown."""
    restaurant_id = event.restaurant_id
    branch_id = event.branch_id
    user_id = event.user_id or ""

    if not restaurant_id:
        return

    try:
        async with get_connection() as conn:
            # Fetch order tax details (if already computed at order time)
            tax_details = await conn.fetch(
                "SELECT * FROM order_tax_details WHERE order_id = $1", order_id,
            )

            total_cgst = sum(float(t["cgst_amount"]) for t in tax_details) if tax_details else 0
            total_sgst = sum(float(t["sgst_amount"]) for t in tax_details) if tax_details else 0
            total_igst = sum(float(t["igst_amount"]) for t in tax_details) if tax_details else 0
            total_tax = total_cgst + total_sgst + total_igst

            # If no order_tax_details yet, try to compute from order's tax_amount
            if total_tax == 0:
                order = await conn.fetchrow(
                    "SELECT tax_amount, subtotal FROM orders WHERE id = $1", order_id,
                )
                if order and order["tax_amount"]:
                    total_tax = float(order["tax_amount"])
                    # Default: split 50/50 as CGST+SGST (intra-state assumption)
                    total_cgst = round(total_tax / 2, 2)
                    total_sgst = total_tax - total_cgst

            revenue_amount = round(amount - total_tax, 2)

            # Determine debit account: Cash (1001) or Bank (1002)
            cash_methods = {"cash", "cod"}
            debit_account = "1001" if method.lower() in cash_methods else "1002"

            # Build journal lines
            lines = [
                {"account_code": debit_account, "debit": round(amount, 2), "credit": 0,
                 "description": f"Payment via {method}"},
                {"account_code": "4001", "debit": 0, "credit": round(revenue_amount, 2),
                 "description": "Sales revenue"},
            ]

            if total_cgst > 0:
                lines.append({"account_code": "2002", "debit": 0, "credit": round(total_cgst, 2),
                               "description": "CGST collected"})
            if total_sgst > 0:
                lines.append({"account_code": "2003", "debit": 0, "credit": round(total_sgst, 2),
                               "description": "SGST collected"})
            if total_igst > 0:
                lines.append({"account_code": "2004", "debit": 0, "credit": round(total_igst, 2),
                               "description": "IGST collected"})

            await _create_journal_entry(
                conn,
                restaurant_id=restaurant_id,
                branch_id=branch_id,
                ref_type="payment",
                ref_id=order_id,
                description=f"Payment for order {order_id} via {method}",
                created_by=user_id,
                lines=lines,
            )

        logger.info("erp_payment_journal_created", order_id=order_id, amount=amount)
    except Exception:
        logger.exception("erp_payment_journal_failed", order_id=order_id)


# ═══════════════════════════════════════════════════════════════════════
# HANDLER: PAYMENT REFUNDED
# 1. Single-entry refund (existing)
# 2. Reverse journal: DR Revenue + GST Payable, CR Cash/Bank
# ═══════════════════════════════════════════════════════════════════════

async def _handle_payment_refunded(event: DomainEvent):
    """Insert refund entry + reverse journal on refund."""
    from app.services.accounting_service import AccountingService

    order_id = event.payload.get("order_id")
    amount = event.payload.get("amount", 0)
    payment_id = event.payload.get("payment_id")

    if not order_id or not amount:
        return

    try:
        # 1. Backward-compat single-entry
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

        # 2. Reverse journal
        await _create_refund_journal(event, order_id, float(amount))

    except Exception:
        logger.exception("erp_refund_record_failed", order_id=order_id)


async def _create_refund_journal(event: DomainEvent, order_id: str, amount: float):
    """Create reverse journal entry for refund."""
    restaurant_id = event.restaurant_id
    branch_id = event.branch_id
    user_id = event.user_id or ""

    if not restaurant_id:
        return

    try:
        async with get_connection() as conn:
            # Look up original tax breakdown
            tax_details = await conn.fetch(
                "SELECT * FROM order_tax_details WHERE order_id = $1", order_id,
            )

            total_cgst = sum(float(t["cgst_amount"]) for t in tax_details) if tax_details else 0
            total_sgst = sum(float(t["sgst_amount"]) for t in tax_details) if tax_details else 0
            total_igst = sum(float(t["igst_amount"]) for t in tax_details) if tax_details else 0
            total_tax = total_cgst + total_sgst + total_igst
            revenue_amount = round(amount - total_tax, 2)

            # Reverse: DR Revenue + GST, CR Bank
            lines = [
                {"account_code": "4001", "debit": round(revenue_amount, 2), "credit": 0,
                 "description": "Revenue reversed (refund)"},
                {"account_code": "1002", "debit": 0, "credit": round(amount, 2),
                 "description": "Refund disbursed"},
            ]

            if total_cgst > 0:
                lines.append({"account_code": "2002", "debit": round(total_cgst, 2), "credit": 0,
                               "description": "CGST reversed"})
            if total_sgst > 0:
                lines.append({"account_code": "2003", "debit": round(total_sgst, 2), "credit": 0,
                               "description": "SGST reversed"})
            if total_igst > 0:
                lines.append({"account_code": "2004", "debit": round(total_igst, 2), "credit": 0,
                               "description": "IGST reversed"})

            await _create_journal_entry(
                conn,
                restaurant_id=restaurant_id,
                branch_id=branch_id,
                ref_type="refund",
                ref_id=order_id,
                description=f"Refund for order {order_id}",
                created_by=user_id,
                lines=lines,
            )

        logger.info("erp_refund_journal_created", order_id=order_id, amount=amount)
    except Exception:
        logger.exception("erp_refund_journal_failed", order_id=order_id)


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
                await _create_journal_entry(
                    conn,
                    restaurant_id=restaurant_id,
                    branch_id=branch_id,
                    ref_type="grn",
                    ref_id=str(grn_id),
                    description=f"Goods received (GRN {grn_id})",
                    created_by=user_id,
                    lines=[
                        {"account_code": "1004", "debit": round(total_amount, 2), "credit": 0,
                         "description": "Inventory purchased"},
                        {"account_code": "2001", "debit": 0, "credit": round(total_amount, 2),
                         "description": "Accounts payable"},
                    ],
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
    logger.info("erp_event_handlers_registered")
