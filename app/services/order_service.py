"""
Order Management Service — the central nervous system of BITTU.

Concurrency control:
  - Distributed lock per order for mutations
  - SERIALIZABLE transactions for financial state changes
  - Idempotency keys to prevent double-creation

Data flow:
  Order Created → Inventory Deducted → Kitchen Order Created → Payment Initiated
  → Payment Completed → Order Served/Delivered

Abuse prevention:
  - Orders can only be cancelled before `preparing` (grace window)
  - Only owners/managers can cancel after confirmation
  - Order amounts are recalculated server-side (never trust client totals)
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.redis import DistributedLock, check_idempotency, set_idempotency, LockError
from app.core.state_machines import OrderStatus, validate_order_transition
from app.core.events import (
    DomainEvent, emit_and_publish,
    ORDER_CREATED, ORDER_CONFIRMED, ORDER_STATUS_CHANGED, ORDER_CANCELLED,
)
from app.core.tenant import tenant_where_clause, tenant_insert_fields
from app.core.exceptions import (
    NotFoundError, ConflictError, ForbiddenError,
    LockAcquisitionError, ValidationError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)


class OrderService:

    # ── Item lookup helper ──

    async def _lookup_item(self, conn, item_id, item_name, user_id):
        """Look up an item by ID or name. Returns the DB row or raises NotFoundError."""
        row = None
        if item_id:
            row = await conn.fetchrow(
                """SELECT "Item_ID", "Item_Name", price, "Available_Status"
                   FROM items WHERE "Item_ID" = $1""",
                item_id,
            )
        if not row and item_name:
            row = await conn.fetchrow(
                """SELECT "Item_ID", "Item_Name", price, "Available_Status"
                   FROM items WHERE "Item_Name" = $1 AND user_id = $2""",
                item_name, user_id,
            )
        if not row:
            raise NotFoundError("Item", str(item_id or item_name))
        if not row["Available_Status"]:
            raise ValidationError(f"Item '{row['Item_Name']}' is currently unavailable")
        return row

    # ── CREATE ORDER ──

    async def create_order(
        self,
        user: UserContext,
        items: list[dict],
        source: str = "pos",
        customer_id: Optional[int] = None,
        table_number: Optional[str] = None,
        delivery_address: Optional[str] = None,
        delivery_phone: Optional[str] = None,
        coupon_id: Optional[int] = None,
        notes: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """
        Create a new order with server-side price calculation.
        Uses idempotency key to prevent double-creation from network retries.
        """
        # Idempotency check
        if idempotency_key:
            existing = await check_idempotency(f"order:{idempotency_key}")
            if existing:
                async with get_connection() as conn:
                    order = await conn.fetchrow(
                        "SELECT * FROM orders WHERE id = $1", existing
                    )
                    return dict(order) if order else {}

        if not items:
            raise ValidationError("Order must contain at least one item")

        tenant = tenant_insert_fields(user)

        async with get_serializable_transaction() as conn:
            # Server-side price calculation — NEVER trust client prices
            subtotal = Decimal("0")
            order_items_data = []

            for item_entry in items:
                item_name = item_entry.get("item_name")
                item_id_input = item_entry.get("item_id")
                quantity = item_entry.get("quantity", 1)
                variant_name = item_entry.get("variant_name")
                variant_id_input = item_entry.get("variant_id")
                addons = item_entry.get("addons", [])
                item_notes = item_entry.get("notes")

                if quantity < 1 or quantity > 100:
                    raise ValidationError(f"Invalid quantity: {quantity}")

                # Fetch authoritative price from DB — lookup by id or name
                if variant_id_input:
                    row = await conn.fetchrow(
                        """
                        SELECT iv.id, iv.name, iv.price, i."Item_ID", i."Item_Name", i."Available_Status"
                        FROM item_variants iv
                        JOIN items i ON i."Item_ID" = iv.item_id
                        WHERE iv.id = $1 AND iv.is_active = true
                        """,
                        variant_id_input,
                    )
                    if row:
                        unit_price = Decimal(str(row["price"]))
                        item_name_full = f"{row['Item_Name']} ({row['name']})"
                        item_id = row["Item_ID"]
                        variant_id = row["id"]
                    else:
                        raise NotFoundError("Variant", str(variant_id_input))
                elif variant_name:
                    row = await conn.fetchrow(
                        """
                        SELECT iv.id, iv.name, iv.price, i."Item_ID", i."Item_Name", i."Available_Status"
                        FROM item_variants iv
                        JOIN items i ON i."Item_ID" = iv.item_id
                        WHERE iv.name = $1 AND i."Item_Name" = $2 AND iv.is_active = true
                        """,
                        variant_name, item_name,
                    )
                    if row:
                        unit_price = Decimal(str(row["price"]))
                        item_name_full = f"{row['Item_Name']} ({row['name']})"
                        item_id = row["Item_ID"]
                        variant_id = row["id"]
                    else:
                        # Fallback to base item if variant not found
                        row = await self._lookup_item(conn, item_id_input, item_name, tenant["user_id"])
                        unit_price = Decimal(str(row["price"]))
                        item_name_full = row["Item_Name"]
                        item_id = row["Item_ID"]
                        variant_id = None
                else:
                    row = await self._lookup_item(conn, item_id_input, item_name, tenant["user_id"])
                    unit_price = Decimal(str(row["price"]))
                    item_name_full = row["Item_Name"]
                    item_id = row["Item_ID"]
                    variant_id = None
                    if not item_name:
                        item_name = row["Item_Name"]

                # Calculate addon prices
                addon_total = Decimal("0")
                if addons:
                    for addon in addons:
                        addon_row = await conn.fetchrow(
                            "SELECT price FROM item_addons WHERE id = $1 AND item_id = $2 AND is_active = true",
                            addon.get("id"), item_id,
                        )
                        if addon_row:
                            addon_total += Decimal(str(addon_row["price"]))

                line_total = (unit_price + addon_total) * quantity
                subtotal += line_total

                order_items_data.append({
                    "item_id": item_id,
                    "variant_id": variant_id,
                    "item_name": item_name,
                    "quantity": quantity,
                    "unit_price": float(unit_price + addon_total),
                    "total_price": float(line_total),
                    "addons": addons,
                    "notes": item_notes,
                })

            # Apply coupon if provided
            discount_amount = Decimal("0")
            if coupon_id:
                discount_amount = await self._calculate_coupon_discount(
                    conn, coupon_id, subtotal, customer_id, tenant["user_id"]
                )

            # Tax calculation (from restaurant settings)
            tax_pct = await self._get_tax_percentage(conn, user.restaurant_id)
            tax_amount = (subtotal - discount_amount) * tax_pct / 100

            total_amount = subtotal - discount_amount + tax_amount

            # Insert order
            order_id = str(uuid.uuid4())
            await conn.execute(
                """
                INSERT INTO orders (
                    id, user_id, branch_id, restaurant_id, customer_id,
                    source, subtotal, tax_amount, discount_amount, total_amount,
                    status, table_number, delivery_address, delivery_phone,
                    coupon_id, notes, items, metadata
                ) VALUES (
                    $1, $2, $3, $4, $5, $6::order_source,
                    $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
                    $17::jsonb, '{}'::jsonb
                )
                """,
                order_id,
                tenant["user_id"],
                tenant.get("branch_id"),
                user.restaurant_id,
                customer_id,
                source,
                float(subtotal),
                float(tax_amount),
                float(discount_amount),
                float(total_amount),
                OrderStatus.PENDING.value,
                table_number,
                delivery_address,
                delivery_phone,
                coupon_id,
                notes,
                "[]",  # items jsonb set via order_items
            )

            # Insert order items
            for oi in order_items_data:
                await conn.execute(
                    """
                    INSERT INTO order_items (
                        order_id, item_id, variant_id, item_name,
                        quantity, unit_price, total_price, addons, notes, user_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
                    """,
                    order_id,
                    oi["item_id"],
                    oi["variant_id"],
                    oi["item_name"],
                    oi["quantity"],
                    oi["unit_price"],
                    oi["total_price"],
                    "[]",  # jsonb
                    oi["notes"],
                    tenant["user_id"],
                )

        # Set idempotency
        if idempotency_key:
            await set_idempotency(f"order:{idempotency_key}", order_id)

        # Emit event
        await emit_and_publish(DomainEvent(
            event_type=ORDER_CREATED,
            payload={
                "order_id": order_id,
                "total_amount": float(total_amount),
                "source": source,
                "item_count": len(order_items_data),
            },
            user_id=user.user_id,
            restaurant_id=user.restaurant_id,
            branch_id=user.branch_id,
        ))

        logger.info("order_created", order_id=order_id, total=float(total_amount), source=source)

        # Non-blocking accounting: order creation must never fail if accounting is unavailable.
        try:
            from app.services.accounting_service import AccountingService

            acct_svc = AccountingService()
            await acct_svc.record_order_sale_double_entry(
                user_id=tenant["user_id"],
                restaurant_id=user.restaurant_id,
                order_id=order_id,
                amount=float(total_amount),
                payment_system_code="CASH_ACCOUNT",
            )
        except Exception:
            logger.exception("order_accounting_post_failed", order_id=order_id)

        return {
            "id": order_id,
            "status": OrderStatus.PENDING.value,
            "subtotal": float(subtotal),
            "tax_amount": float(tax_amount),
            "discount_amount": float(discount_amount),
            "total_amount": float(total_amount),
            "items": order_items_data,
        }

    # ── UPDATE ORDER (GENERAL) ──

    async def update_order(
        self,
        user: UserContext,
        order_id: str,
        status: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> dict:
        """Update order fields. Handles status transition + notes."""
        async with get_connection() as conn:
            owner_id = user.owner_id if user.is_branch_user else user.user_id
            order = await conn.fetchrow(
                "SELECT id, status FROM orders WHERE id = $1 AND user_id = $2",
                order_id, owner_id,
            )
            if not order:
                raise NotFoundError("Order", order_id)

            # Update notes if provided
            if notes is not None:
                await conn.execute(
                    "UPDATE orders SET notes = $1, updated_at = now() WHERE id = $2",
                    notes, order_id,
                )

        # Update status if provided
        if status:
            return await self.update_status(user=user, order_id=order_id, new_status=status)

        return await self.get_order_detail(user=user, order_id=order_id)

    # ── UPDATE ORDER STATUS ──

    async def update_status(
        self,
        user: UserContext,
        order_id: str,
        new_status: str,
    ) -> dict:
        """
        Transition order to a new status with concurrency protection.
        Uses distributed lock + state machine validation.
        """
        try:
            async with DistributedLock(f"order:{order_id}", timeout=10):
                async with get_serializable_transaction() as conn:
                    # Fetch current state with row lock
                    order = await conn.fetchrow(
                        """
                        SELECT id, status, user_id, branch_id, total_amount, source
                        FROM orders
                        WHERE id = $1 AND user_id = $2
                        FOR UPDATE
                        """,
                        order_id, user.owner_id if user.is_branch_user else user.user_id,
                    )

                    if not order:
                        raise NotFoundError("Order", order_id)

                    current = order["status"]
                    target = validate_order_transition(current, new_status)

                    # Idempotent: no-op if already in the target state
                    if target is None:
                        return {"id": order_id, "status": current}

                    # Business rules for cancellation
                    if target == OrderStatus.CANCELLED:
                        await self._validate_cancellation(user, order)

                    now = datetime.now(timezone.utc)
                    await conn.execute(
                        "UPDATE orders SET status = $1, updated_at = $2 WHERE id = $3",
                        target.value, now, order_id,
                    )

                    # Side effects based on new status
                    event_type = ORDER_STATUS_CHANGED
                    if target == OrderStatus.CONFIRMED:
                        event_type = ORDER_CONFIRMED
                    elif target == OrderStatus.CANCELLED:
                        event_type = ORDER_CANCELLED

                await emit_and_publish(DomainEvent(
                    event_type=event_type,
                    payload={
                        "order_id": order_id,
                        "from_status": current,
                        "to_status": target.value,
                    },
                    user_id=user.user_id,
                    restaurant_id=user.restaurant_id,
                    branch_id=user.branch_id,
                ))

                logger.info(
                    "order_status_changed",
                    order_id=order_id,
                    from_status=current,
                    to_status=target.value,
                )

                return {"id": order_id, "status": target.value}

        except LockError:
            raise LockAcquisitionError(f"order:{order_id}")

    # ── GET ORDERS ──

    async def get_orders(
        self,
        user: UserContext,
        status: Optional[str] = None,
        source: Optional[str] = None,
        branch_id: Optional[str] = None,
        order_type: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Fetch orders with tenant isolation. Optionally filter by branch, status, dates, etc."""
        clause, params = tenant_where_clause(user, "o")

        conditions = [clause]
        if status:
            params.append(status)
            conditions.append(f"o.status = ${len(params)}")
        if source:
            params.append(source)
            conditions.append(f"o.source = ${len(params)}::order_source")
        if branch_id:
            params.append(branch_id)
            conditions.append(f"o.branch_id = ${len(params)}")
        if order_type:
            params.append(order_type)
            conditions.append(f"o.source = ${len(params)}::order_source")
        if from_date:
            params.append(from_date)
            conditions.append(f"o.created_at >= ${len(params)}::date")
        if to_date:
            params.append(to_date)
            conditions.append(f"o.created_at < (${len(params)}::date + INTERVAL '1 day')")

        params.extend([limit, offset])
        where = " AND ".join(conditions)

        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""
                SELECT o.*, c.name as customer_name, c.phone_number as customer_phone
                FROM orders o
                LEFT JOIN customers c ON c.id = o.customer_id
                WHERE {where}
                ORDER BY o.created_at DESC
                LIMIT ${len(params) - 1} OFFSET ${len(params)}
                """,
                *params,
            )
            return [dict(r) for r in rows]

    async def get_order_detail(self, user: UserContext, order_id: str) -> dict:
        """Fetch order with items. Tenant-scoped."""
        async with get_connection() as conn:
            order = await conn.fetchrow(
                """
                SELECT o.*, c.name as customer_name, c.phone_number as customer_phone
                FROM orders o
                LEFT JOIN customers c ON c.id = o.customer_id
                WHERE o.id = $1 AND o.user_id = $2
                """,
                order_id, user.owner_id if user.is_branch_user else user.user_id,
            )
            if not order:
                raise NotFoundError("Order", order_id)

            items = await conn.fetch(
                "SELECT * FROM order_items WHERE order_id = $1",
                order_id,
            )
            result = dict(order)
            result["order_items"] = [dict(i) for i in items]
            return result

    # ── PRIVATE HELPERS ──

    async def _validate_cancellation(self, user: UserContext, order: dict):
        """Only owner/manager can cancel after confirmation."""
        status = OrderStatus(order["status"])
        if status in (OrderStatus.PREPARING, OrderStatus.READY):
            if user.role not in ("owner", "manager"):
                raise ForbiddenError("Only owner/manager can cancel orders in preparation")

    async def _calculate_coupon_discount(
        self, conn, coupon_id: int, subtotal: Decimal, customer_id: Optional[int], user_id: str
    ) -> Decimal:
        coupon = await conn.fetchrow(
            """
            SELECT * FROM coupons
            WHERE id = $1 AND user_id = $2 AND is_active = true
            AND (valid_from IS NULL OR valid_from <= now())
            AND (valid_until IS NULL OR valid_until >= now())
            """,
            coupon_id, user_id,
        )
        if not coupon:
            return Decimal("0")

        if coupon["min_order_value"] and subtotal < Decimal(str(coupon["min_order_value"])):
            return Decimal("0")

        if coupon["usage_limit"] and coupon["times_used"] >= coupon["usage_limit"]:
            return Decimal("0")

        discount_value = Decimal(str(coupon["discount_value"]))
        if coupon["type"] == "percentage":
            discount = subtotal * discount_value / 100
            max_disc = Decimal(str(coupon["max_discount"])) if coupon["max_discount"] else None
            if max_disc and max_disc > 0:
                discount = min(discount, max_disc)
        else:
            discount = discount_value

        return min(discount, subtotal)  # Never exceed subtotal

    async def _get_tax_percentage(self, conn, restaurant_id: Optional[str]) -> Decimal:
        if not restaurant_id:
            return Decimal("0")
        row = await conn.fetchrow(
            "SELECT tax_percentage FROM restaurant_settings WHERE restaurant_id = $1",
            restaurant_id,
        )
        return Decimal(str(row["tax_percentage"])) if row else Decimal("5.0")
