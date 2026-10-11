"""
Inventory Management Service.

Critical concurrency requirements:
  - Stock deductions are atomic (no negative stock)
  - Race conditions between concurrent orders prevented via row-level locks
  - Deductions happen on order confirmation, rollback on cancellation
  - Purchase orders increase stock atomically

Approach:
  - SELECT FOR UPDATE on ingredient rows during deduction
  - CHECK constraint (current_stock >= 0) as final guard
  - Transaction log for full audit trail
"""
from decimal import Decimal
from typing import Optional

from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.redis import DistributedLock, LockError, cache_delete_pattern
from app.core.events import (
    DomainEvent, emit_and_publish,
    INVENTORY_DEDUCTED, INVENTORY_RESTORED, INVENTORY_LOW_STOCK, INVENTORY_PURCHASED,
)
from app.core.tenant import tenant_where_clause, tenant_insert_fields
from app.core.exceptions import (
    NotFoundError, InventoryError, LockAcquisitionError, ValidationError,
)
from app.core.logging import get_logger
from app.services.inventory_event_service import inventory_event_service
from app.services.inventory_write import inventory_transaction, unit_factor

logger = get_logger(__name__)


class InventoryService:

    # ── DEDUCT STOCK ON ORDER ──

    async def deduct_for_order(
        self,
        user_id: str,
        order_id: str,
        order_items: list[dict],
    ) -> dict:
        """
        Deduct ingredient stock for all items in an order.
        Called when order transitions to CONFIRMED.

        Uses SERIALIZABLE transaction + row-level locks to prevent:
        - Two concurrent orders depleting the same ingredient
        - Negative stock from race conditions
        """
        deductions = []

        try:
            async with DistributedLock(f"inventory:order:{order_id}", timeout=15):
                async with get_serializable_transaction() as conn:
                    for oi in order_items:
                        item_id = oi.get("item_id")
                        quantity = oi.get("quantity", 1)

                        # Get item-ingredient mappings
                        mappings = await conn.fetch(
                            """
                            SELECT ii.ingredient_id, ii.quantity_used, ii.unit,
                                   i.name as ingredient_name, i.current_stock,
                                   i.minimum_stock, i.unit as ingredient_unit
                            FROM item_ingredients ii
                            JOIN ingredients i ON i.id = ii.ingredient_id
                            WHERE ii.item_id = $1
                            FOR UPDATE OF i
                            """,
                            item_id,
                        )

                        for mapping in mappings:
                            required = Decimal(str(mapping["quantity_used"])) * quantity
                            current = Decimal(str(mapping["current_stock"]))

                            if current < required:
                                raise InventoryError(
                                    f"Insufficient stock for {mapping['ingredient_name']}: "
                                    f"need {required} {mapping['ingredient_unit']}, "
                                    f"have {current} {mapping['ingredient_unit']}"
                                )

                            new_stock = current - required

                            await conn.execute(
                                """
                                UPDATE ingredients
                                SET current_stock = $1, stock_quantity = $1, updated_at = now()
                                WHERE id = $2
                                """,
                                float(new_stock),
                                str(mapping["ingredient_id"]),
                            )

                            # Log transaction
                            await conn.execute(
                                """
                                INSERT INTO inventory_transactions
                                    (restaurant_id, ingredient_id, type, quantity, reference_id, performed_by, notes)
                                VALUES (
                                    (SELECT restaurant_id FROM ingredients WHERE id = $1),
                                    $1, 'consumption'::inventory_txn_type, $2, $3, $4,
                                    $5
                                )
                                """,
                                str(mapping["ingredient_id"]),
                                float(-required),
                                order_id,
                                user_id,
                                f"Order {order_id}: {mapping['ingredient_name']}",
                            )

                            deductions.append({
                                "ingredient_id": str(mapping["ingredient_id"]),
                                "ingredient_name": mapping["ingredient_name"],
                                "deducted": float(required),
                                "remaining": float(new_stock),
                            })

                            # Check low stock alert
                            min_stock = float(mapping["minimum_stock"] or 0)
                            if float(new_stock) <= min_stock:
                                await emit_and_publish(DomainEvent(
                                    event_type=INVENTORY_LOW_STOCK,
                                    payload={
                                        "ingredient_id": str(mapping["ingredient_id"]),
                                        "ingredient_name": mapping["ingredient_name"],
                                        "current_stock": float(new_stock),
                                        "minimum_stock": min_stock,
                                    },
                                ))

        except LockError:
            raise LockAcquisitionError(f"inventory:order:{order_id}")

        await emit_and_publish(DomainEvent(
            event_type=INVENTORY_DEDUCTED,
            payload={"order_id": order_id, "deductions": deductions},
        ))

        # Invalidate inventory cache
        await cache_delete_pattern("inventory:*")

        logger.info("inventory_deducted", order_id=order_id, items=len(deductions))
        return {"deductions": deductions}

    # ── RESTORE STOCK ON CANCELLATION ──

    async def restore_for_order(
        self,
        user_id: str,
        order_id: str,
    ) -> dict:
        """
        Restore ingredient stock when an order is cancelled.
        Reverses all consumption transactions for this order.
        """
        async with get_serializable_transaction() as conn:
            # Find all consumption transactions for this order
            txns = await conn.fetch(
                """
                SELECT ingredient_id, quantity
                FROM inventory_transactions
                WHERE reference_id = $1 AND type = 'consumption'::inventory_txn_type
                """,
                order_id,
            )

            restorations = []
            for txn in txns:
                restore_qty = abs(float(txn["quantity"]))
                await conn.execute(
                    """
                    UPDATE ingredients
                    SET current_stock = current_stock + $1, stock_quantity = stock_quantity + $1, updated_at = now()
                    WHERE id = $2
                    """,
                    restore_qty,
                    str(txn["ingredient_id"]),
                )

                await conn.execute(
                    """
                    INSERT INTO inventory_transactions
                        (restaurant_id, ingredient_id, type, quantity, reference_id, performed_by, notes)
                    VALUES (
                        (SELECT restaurant_id FROM ingredients WHERE id = $1),
                        $1, 'return'::inventory_txn_type, $2, $3, $4,
                        'Restored from cancelled order'
                    )
                    """,
                    str(txn["ingredient_id"]),
                    restore_qty,
                    order_id,
                    user_id,
                )
                restorations.append({
                    "ingredient_id": str(txn["ingredient_id"]),
                    "restored": restore_qty,
                })

        await emit_and_publish(DomainEvent(
            event_type=INVENTORY_RESTORED,
            payload={"order_id": order_id, "restorations": restorations},
        ))

        await cache_delete_pattern("inventory:*")
        logger.info("inventory_restored", order_id=order_id, items=len(restorations))
        return {"restorations": restorations}

    # ── PURCHASE ORDER RECEIVE ──

    async def receive_purchase_order(
        self, user: UserContext, purchase_order_id: int,
        items: Optional[list[dict]] = None,
    ) -> dict:
        """Receive approved POs. Explicit quantities are cumulative in PO units.

        Omitted lines stay unchanged; items=None receives all remaining stock.
        Repeating a cumulative receipt is a no-op, including after completion.
        """
        clause, params = tenant_where_clause(user)
        params.append(int(purchase_order_id))
        async with inventory_transaction() as (conn, events):
            po = await conn.fetchrow(
                f"SELECT * FROM purchase_orders WHERE {clause} AND id=${len(params)} FOR UPDATE",
                *params,
            )
            if not po:
                raise NotFoundError("Purchase order", purchase_order_id)
            if po["approval_status"] != "approved":
                raise ValidationError("purchase order must be approved before receiving stock")
            if po["status"] in ("cancelled", "rejected"):
                raise ValidationError("cannot receive a cancelled or rejected purchase order")
            lines = await conn.fetch(
                "SELECT * FROM purchase_order_items WHERE purchase_order_id=$1 ORDER BY ingredient_id, id FOR UPDATE",
                int(purchase_order_id),
            )
            if not lines:
                raise ValidationError("purchase order has no items")
            requested = None if items is None else {int(i["purchase_order_item_id"]): i for i in items}
            if requested is not None:
                if len(requested) != len(items):
                    raise ValidationError("duplicate purchase order receipt lines")
                if set(requested) - {i["id"] for i in lines}:
                    raise ValidationError("receipt contains an item outside this purchase order")
            updated = 0
            complete = True
            for line in lines:
                ordered = Decimal(str(line["quantity_ordered"] if line["quantity_ordered"] is not None else line["quantity"] or 0))
                previous = Decimal(str(line["quantity_received"] or 0))
                request = (requested or {}).get(line["id"], {})
                target = ordered if requested is None else Decimal(str(request.get("quantity_received", previous)))
                if not target.is_finite() or target < previous or target > ordered:
                    raise ValidationError("received quantity must be between the previous receipt and ordered quantity")
                complete = complete and target == ordered
                delta = target - previous
                if delta == 0:
                    continue
                ing = await conn.fetchrow(
                    "SELECT id, unit, restaurant_id, track_batches FROM ingredients "
                    "WHERE id=$1 AND restaurant_id=$2::uuid FOR UPDATE",
                    line["ingredient_id"], user.restaurant_id,
                )
                if not ing:
                    raise NotFoundError("ingredient", line["ingredient_id"])
                factor = await unit_factor(conn, restaurant_id=user.restaurant_id, ingredient_id=ing["id"],
                                           from_unit=line["unit"], to_unit=ing["unit"])
                quantity = delta * factor
                cost = Decimal(str(line["unit_price"] if line["unit_price"] is not None else line["unit_cost"] or 0)) / factor
                branch_id = str(po["branch_id"]) if po["branch_id"] else None
                batch_id = None
                if ing["track_batches"]:
                    batch_id = str(await conn.fetchval(
                        """INSERT INTO inventory_batches
                           (restaurant_id, branch_id, ingredient_id, batch_number,
                            received_quantity, remaining_quantity, unit, unit_cost, expiry_date, created_by)
                           VALUES ($1::uuid,$2::uuid,$3,$4,$5,0,$6,$7,$8,$9) RETURNING id""",
                        user.restaurant_id, branch_id, ing["id"],
                        request.get("batch_number") or f"PO-{purchase_order_id}-{line['id']}-{target}",
                        quantity, ing["unit"], cost, request.get("expiry_date"), user.user_id,
                    ))
                await inventory_event_service.append_event(
                    conn=conn, event_buffer=events, restaurant_id=user.restaurant_id,
                    branch_id=branch_id, ingredient_id=ing["id"], event_type=INVENTORY_PURCHASED,
                    quantity_in=quantity, unit_cost=cost, batch_id=batch_id,
                    reference_type="purchase_order", reference_id=str(purchase_order_id),
                    dedup_key=f"po:{purchase_order_id}:item:{line['id']}:received:{target.normalize()}",
                    source="purchase_order", created_by=user.user_id,
                )
                await conn.execute("UPDATE purchase_order_items SET quantity_received=$1 WHERE id=$2", target, line["id"])
                await conn.execute(
                    "UPDATE ingredients SET cost_per_unit=CASE WHEN $1>0 THEN $1 ELSE cost_per_unit END WHERE id=$2",
                    cost, ing["id"],
                )
                updated += 1
            status = "received" if complete else "partial"
            await conn.execute(
                "UPDATE purchase_orders SET status=$1, received_at=CASE WHEN $1='received' THEN COALESCE(received_at,NOW()) "
                "ELSE NULL END, updated_at=NOW() WHERE id=$2", status, int(purchase_order_id),
            )
        await cache_delete_pattern("inventory:*")
        return {"status": status, "items_updated": updated}

    # ── GET STOCK LEVELS ──

    async def get_stock_levels(
        self,
        user: UserContext,
        low_stock_only: bool = False,
    ) -> list[dict]:
        clause, params = tenant_where_clause(user, "i")
        extra = ""
        if low_stock_only:
            extra = " AND i.current_stock <= COALESCE(i.minimum_stock, 0)"

        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""
                SELECT i.id, i.name, i.unit, i.current_stock, i.minimum_stock,
                       i.cost_per_unit, i.supplier, i.is_active
                FROM ingredients i
                WHERE {clause} {extra}
                ORDER BY i.name
                """,
                *params,
            )
            return [dict(r) for r in rows]
