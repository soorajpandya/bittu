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
    INVENTORY_DEDUCTED, INVENTORY_RESTORED, INVENTORY_LOW_STOCK,
)
from app.core.tenant import tenant_where_clause, tenant_insert_fields
from app.core.exceptions import (
    NotFoundError, InventoryError, LockAcquisitionError, ValidationError,
)
from app.core.logging import get_logger

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
        self,
        user: UserContext,
        purchase_order_id: str,
    ) -> dict:
        """Mark a purchase order as received and increase stock."""
        async with get_serializable_transaction() as conn:
            po = await conn.fetchrow(
                "SELECT id, status, user_id FROM purchase_orders WHERE id = $1 AND user_id = $2 FOR UPDATE",
                purchase_order_id,
                user.owner_id if user.is_branch_user else user.user_id,
            )
            if not po:
                raise NotFoundError("Purchase order", purchase_order_id)
            if po["status"] == "received":
                raise ValidationError("Purchase order already received")

            items = await conn.fetch(
                "SELECT * FROM purchase_order_items WHERE purchase_order_id = $1",
                purchase_order_id,
            )

            for item in items:
                await conn.execute(
                    """
                    UPDATE ingredients
                    SET current_stock = current_stock + $1,
                        stock_quantity = stock_quantity + $1,
                        cost_per_unit = CASE WHEN $2 > 0 THEN $2 ELSE cost_per_unit END,
                        updated_at = now()
                    WHERE id = $3
                    """,
                    float(item["quantity"]),
                    float(item["unit_cost"]),
                    str(item["ingredient_id"]),
                )

                await conn.execute(
                    """
                    INSERT INTO inventory_transactions
                        (restaurant_id, ingredient_id, type, quantity, reference_id, performed_by, notes)
                    VALUES (
                        (SELECT restaurant_id FROM ingredients WHERE id = $1),
                        $1, 'purchase'::inventory_txn_type, $2, $3, $4,
                        'Purchase order received'
                    )
                    """,
                    str(item["ingredient_id"]),
                    float(item["quantity"]),
                    purchase_order_id,
                    user.user_id,
                )

            await conn.execute(
                "UPDATE purchase_orders SET status = 'received', received_at = now() WHERE id = $1",
                purchase_order_id,
            )

        await cache_delete_pattern("inventory:*")
        return {"status": "received", "items_updated": len(items)}

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
