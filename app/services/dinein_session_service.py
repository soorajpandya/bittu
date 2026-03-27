"""
Dine-In Session Service — QR-based ordering with strict session isolation.

Core principles:
  - Session = primary boundary for diners (not table)
  - One ACTIVE order per session (reuse, append items)
  - WebSocket broadcasts scoped to session, not table
  - Merge is explicit, user-controlled, deterministic
  - Idempotency via request_id on every mutation
  - Concurrency-safe via distributed locks + SERIALIZABLE txns
"""
import uuid
import json
import secrets
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional

from app.core.database import get_connection, get_serializable_transaction
from app.core.redis import DistributedLock, LockError, get_redis
from app.core.events import (
    DomainEvent, emit_and_publish,
    TABLE_ORDER_PLACED, TABLE_CART_UPDATED, TABLE_CALL_WAITER,
    KITCHEN_ORDER_CREATED,
)
from app.core.exceptions import (
    NotFoundError, ConflictError, ValidationError, ForbiddenError,
    LockAcquisitionError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

# ── Constants ────────────────────────────────────────────────
SESSION_EXPIRY_HOURS = 4
INACTIVITY_TIMEOUT_MINUTES = 120  # 2 hours
IDEMPOTENCY_TTL_SECONDS = 86400   # 24h

# ── New event types ──────────────────────────────────────────
SESSION_CREATED = "dinein.session_created"
SESSION_RESTORED = "dinein.session_restored"
SESSION_EXPIRED = "dinein.session_expired"
SESSION_CLOSED = "dinein.session_closed"
SESSION_MERGED = "dinein.session_merged"
DINEIN_ORDER_UPDATED = "dinein.order_updated"
DINEIN_ITEM_ADDED = "dinein.item_added"


class DineInSessionService:
    """Production-grade dine-in session management with isolation guarantees."""

    # ══════════════════════════════════════════════════════════
    # 1. SESSION LIFECYCLE
    # ══════════════════════════════════════════════════════════

    async def scan_qr(
        self,
        restaurant_id: str,
        table_id: str,
        device_id: str,
        client_session_token: Optional[str] = None,
    ) -> dict:
        """
        Customer scans QR code.
        - If client sends a valid session_token → restore session
        - Otherwise create a new session
        - Same table can have multiple independent sessions
        """
        # ── Try to restore existing session from client token ──
        if client_session_token:
            restored = await self._try_restore_session(client_session_token)
            if restored:
                await self._touch_activity(restored["id"])
                return {
                    "session_id": restored["id"],
                    "session_token": restored["session_token"],
                    "table": restored["table"],
                    "restaurant": restored["restaurant"],
                    "is_new": False,
                    "active_order_id": restored.get("active_order_id"),
                }

        # ── Validate table ──
        async with get_connection() as conn:
            table = await conn.fetchrow(
                """
                SELECT rt.id, rt.table_number, rt.capacity, rt.user_id, rt.restaurant_id,
                       r.id AS rest_id, r.name AS rest_name, r.logo_url, r.phone, r.address, r.city
                FROM restaurant_tables rt
                LEFT JOIN restaurants r ON r.id = rt.restaurant_id
                WHERE rt.id = $1::uuid AND rt.is_active = true
                  AND (rt.restaurant_id = $2::uuid OR rt.user_id = $2::uuid)
                """,
                table_id, restaurant_id,
            )
            if not table:
                # Fallback: just by table_id
                table = await conn.fetchrow(
                    """
                    SELECT rt.id, rt.table_number, rt.capacity, rt.user_id, rt.restaurant_id,
                           r.id AS rest_id, r.name AS rest_name, r.logo_url, r.phone, r.address, r.city
                    FROM restaurant_tables rt
                    LEFT JOIN restaurants r ON r.id = rt.restaurant_id
                    WHERE rt.id = $1::uuid AND rt.is_active = true
                    """,
                    table_id,
                )
            if not table:
                raise NotFoundError("Table", table_id)

        # ── Create new session ──
        session_id = str(uuid.uuid4())
        session_token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=SESSION_EXPIRY_HOURS)
        owner_id = str(table["user_id"])
        actual_restaurant_id = str(table["restaurant_id"]) if table["restaurant_id"] else None

        async with get_serializable_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO dine_in_sessions (
                    id, table_id, restaurant_id, user_id, branch_id,
                    session_token, device_id, guest_count, status,
                    last_activity_at, expires_at
                ) VALUES ($1, $2, $3, $4, NULL, $5, $6, 1, 'active', $7, $8)
                """,
                session_id, table_id, actual_restaurant_id,
                owner_id, session_token, device_id, now, expires_at,
            )

            # Ensure table is marked running
            await conn.execute(
                """
                UPDATE restaurant_tables
                SET status = 'running', is_occupied = true, occupied_since = COALESCE(occupied_since, $1)
                WHERE id = $2
                """,
                now, table_id,
            )

        table_info = {
            "id": str(table["id"]),
            "table_number": table["table_number"],
            "capacity": table["capacity"],
        }
        restaurant_info = {
            "id": str(table["rest_id"]) if table["rest_id"] else None,
            "name": table["rest_name"],
            "logo_url": table.get("logo_url"),
            "phone": table.get("phone"),
            "address": table.get("address"),
            "city": table.get("city"),
        } if table["rest_id"] else {}

        await emit_and_publish(DomainEvent(
            event_type=SESSION_CREATED,
            payload={
                "session_id": session_id,
                "table_id": table_id,
                "table_number": table["table_number"],
                "device_id": device_id,
            },
            restaurant_id=actual_restaurant_id,
        ))

        return {
            "session_id": session_id,
            "session_token": session_token,
            "table": table_info,
            "restaurant": restaurant_info,
            "is_new": True,
            "active_order_id": None,
        }

    async def get_session_state(self, session_token: str) -> dict:
        """
        Get full session state including active order snapshot.
        Used on reconnect / page refresh.
        """
        session = await self._get_valid_session(session_token)
        sid = session["id"]

        async with get_connection() as conn:
            # Get active order if any
            order = None
            if session.get("active_order_id"):
                order = await self._get_order_snapshot(conn, str(session["active_order_id"]))

            # Get cart
            cart_items = await conn.fetch(
                "SELECT * FROM table_session_carts WHERE session_id = $1 ORDER BY created_at",
                sid,
            )

            # Get linked sessions (post-merge)
            linked = await conn.fetch(
                """
                SELECT so.session_id, so.role, ds.device_id
                FROM session_orders so
                JOIN dine_in_sessions ds ON ds.id = so.session_id
                WHERE so.order_id = $1 AND so.session_id != $2
                """,
                str(session["active_order_id"]) if session.get("active_order_id") else "00000000-0000-0000-0000-000000000000",
                sid,
            )

        return {
            "session_id": sid,
            "status": session["status"],
            "table_id": str(session["table_id"]),
            "active_order": order,
            "cart": [dict(c) for c in cart_items],
            "linked_sessions": [dict(l) for l in linked],
            "expires_at": session["expires_at"].isoformat() if session["expires_at"] else None,
        }

    async def close_session(self, session_token: str, reason: str = "completed") -> dict:
        """
        Close a session. Order remains intact but session loses access.
        reason: 'completed' | 'manual_exit'
        """
        session = await self._get_valid_session(session_token)
        sid = session["id"]

        try:
            async with DistributedLock(f"dinein_session:{sid}", timeout=10):
                async with get_serializable_transaction() as conn:
                    new_status = "completed" if reason == "completed" else "cancelled"
                    await conn.execute(
                        """
                        UPDATE dine_in_sessions
                        SET status = $1, ended_at = now()
                        WHERE id = $2 AND status = 'active'
                        """,
                        new_status, sid,
                    )

                    # Check if this was the last active session on the table
                    remaining = await conn.fetchval(
                        """
                        SELECT COUNT(*) FROM dine_in_sessions
                        WHERE table_id = $1 AND status = 'active' AND id != $2
                        """,
                        str(session["table_id"]), sid,
                    )

                    if remaining == 0:
                        # Free the table
                        await conn.execute(
                            """
                            UPDATE restaurant_tables
                            SET status = 'blank', is_occupied = false,
                                occupied_since = NULL, session_token = NULL, current_order_id = NULL
                            WHERE id = $1
                            """,
                            str(session["table_id"]),
                        )

            await emit_and_publish(DomainEvent(
                event_type=SESSION_CLOSED,
                payload={"session_id": sid, "reason": reason},
                restaurant_id=str(session["restaurant_id"]) if session.get("restaurant_id") else None,
            ))

            return {"status": new_status, "session_id": sid}

        except LockError:
            raise LockAcquisitionError(f"dinein_session:{sid}")

    async def expire_inactive_sessions(self) -> int:
        """
        Background task: expire sessions with no activity past timeout.
        Returns count of expired sessions.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=INACTIVITY_TIMEOUT_MINUTES)
        async with get_connection() as conn:
            result = await conn.execute(
                """
                UPDATE dine_in_sessions
                SET status = 'expired', ended_at = now()
                WHERE status = 'active'
                  AND (last_activity_at < $1 OR expires_at < now())
                """,
                cutoff,
            )
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                logger.info("sessions_expired", count=count)
            return count

    # ══════════════════════════════════════════════════════════
    # 2. CART OPERATIONS (Session-Scoped, Idempotent)
    # ══════════════════════════════════════════════════════════

    async def add_to_cart(
        self,
        session_token: str,
        item_id: int,
        quantity: int = 1,
        variant_id: Optional[str] = None,
        addons: Optional[list] = None,
        extras: Optional[list] = None,
        notes: Optional[str] = None,
        device_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        """
        Add item to session cart.
        - Idempotent via request_id
        - Same item → increment quantity (no duplicate rows)
        - Serialized via distributed lock
        """
        session = await self._get_valid_session(session_token)
        sid = session["id"]

        # ── Idempotency check ──
        if request_id:
            cached = await self._check_idempotency(request_id, sid)
            if cached is not None:
                return cached

        try:
            async with DistributedLock(f"cart:{sid}", timeout=5):
                async with get_serializable_transaction() as conn:
                    # Server-side price lookup
                    item = await conn.fetchrow(
                        'SELECT "Item_ID", "Item_Name", price, "Available_Status" FROM items WHERE "Item_ID" = $1',
                        item_id,
                    )
                    if not item or not item["Available_Status"]:
                        raise ValidationError("Item not available")

                    item_name = item["Item_Name"]
                    unit_price = float(item["price"])
                    variant_name = None

                    if variant_id:
                        variant = await conn.fetchrow(
                            "SELECT name, price FROM item_variants WHERE id = $1 AND item_id = $2 AND is_active = true",
                            variant_id, item_id,
                        )
                        if variant:
                            unit_price = float(variant["price"])
                            variant_name = variant["name"]

                    addon_total = sum(float(a.get("price", 0)) for a in (addons or []))
                    extra_total = sum(float(e.get("price", 0)) for e in (extras or []))
                    line_price = unit_price + addon_total + extra_total

                    # ── Quantity merging: same item+variant → increment ──
                    existing = await conn.fetchrow(
                        """
                        SELECT id, quantity, unit_price FROM table_session_carts
                        WHERE session_id = $1 AND item_id = $2
                          AND COALESCE(variant_id, '') = COALESCE($3, '')
                          AND COALESCE(notes, '') = COALESCE($4, '')
                        """,
                        sid, item_id, variant_id, notes,
                    )

                    if existing:
                        new_qty = existing["quantity"] + quantity
                        new_total = line_price * new_qty
                        await conn.execute(
                            """
                            UPDATE table_session_carts
                            SET quantity = $1, total_price = $2, addons = $3::jsonb, extras = $4::jsonb
                            WHERE id = $5
                            """,
                            new_qty, new_total,
                            json.dumps(addons or []), json.dumps(extras or []),
                            str(existing["id"]),
                        )
                        cart_item_id = str(existing["id"])
                        result_qty = new_qty
                        total_price = new_total
                    else:
                        total_price = line_price * quantity
                        cart_item_id = str(uuid.uuid4())
                        await conn.execute(
                            """
                            INSERT INTO table_session_carts (
                                id, session_id, item_id, variant_id,
                                item_name, variant_name, quantity,
                                unit_price, total_price,
                                addons, extras, notes, added_by, request_id
                            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb,$12,$13,$14)
                            """,
                            cart_item_id, sid, item_id, variant_id,
                            item_name, variant_name, quantity,
                            unit_price, total_price,
                            json.dumps(addons or []), json.dumps(extras or []),
                            notes, device_id, request_id,
                        )
                        result_qty = quantity

                    # Touch activity
                    await conn.execute(
                        "UPDATE dine_in_sessions SET last_activity_at = now() WHERE id = $1",
                        sid,
                    )

                result = {
                    "cart_item_id": cart_item_id,
                    "item_name": item_name,
                    "quantity": result_qty,
                    "total_price": total_price,
                    "status": "added",
                }

                # Save idempotency
                if request_id:
                    await self._save_idempotency(request_id, sid, result)

                # Emit to session channel only
                await self._emit_session(
                    session,
                    TABLE_CART_UPDATED,
                    {"session_id": sid, "action": "item_added", "item_name": item_name, "quantity": result_qty},
                )

                return result

        except LockError:
            raise LockAcquisitionError(f"cart:{sid}")

    async def update_cart_item(
        self,
        session_token: str,
        cart_item_id: str,
        quantity: Optional[int] = None,
        addons: Optional[list] = None,
        extras: Optional[list] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        """Update quantity or customizations of a cart item."""
        session = await self._get_valid_session(session_token)
        sid = session["id"]

        if request_id:
            cached = await self._check_idempotency(request_id, sid)
            if cached is not None:
                return cached

        try:
            async with DistributedLock(f"cart:{sid}", timeout=5):
                async with get_serializable_transaction() as conn:
                    row = await conn.fetchrow(
                        "SELECT * FROM table_session_carts WHERE id = $1 AND session_id = $2",
                        cart_item_id, sid,
                    )
                    if not row:
                        raise NotFoundError("CartItem", cart_item_id)

                    new_qty = quantity if quantity is not None else row["quantity"]
                    if new_qty <= 0:
                        await conn.execute(
                            "DELETE FROM table_session_carts WHERE id = $1", cart_item_id,
                        )
                        result = {"status": "removed", "cart_item_id": cart_item_id}
                    else:
                        base = float(row["unit_price"])
                        addon_total = sum(float(a.get("price", 0)) for a in (addons or []))
                        extra_total = sum(float(e.get("price", 0)) for e in (extras or []))
                        total_price = (base + addon_total + extra_total) * new_qty

                        await conn.execute(
                            """
                            UPDATE table_session_carts
                            SET quantity = $1, total_price = $2, addons = $3::jsonb, extras = $4::jsonb
                            WHERE id = $5
                            """,
                            new_qty, total_price,
                            json.dumps(addons or json.loads(row["addons"]) if row["addons"] else []),
                            json.dumps(extras or json.loads(row["extras"]) if row["extras"] else []),
                            cart_item_id,
                        )
                        result = {"status": "updated", "cart_item_id": cart_item_id, "quantity": new_qty, "total_price": total_price}

                    await conn.execute(
                        "UPDATE dine_in_sessions SET last_activity_at = now() WHERE id = $1", sid,
                    )

                if request_id:
                    await self._save_idempotency(request_id, sid, result)

                await self._emit_session(session, TABLE_CART_UPDATED, {"session_id": sid, "action": "cart_updated"})
                return result

        except LockError:
            raise LockAcquisitionError(f"cart:{sid}")

    async def remove_cart_item(self, session_token: str, cart_item_id: str) -> dict:
        session = await self._get_valid_session(session_token)
        sid = session["id"]
        async with get_connection() as conn:
            deleted = await conn.execute(
                "DELETE FROM table_session_carts WHERE id = $1 AND session_id = $2",
                cart_item_id, sid,
            )
            await conn.execute(
                "UPDATE dine_in_sessions SET last_activity_at = now() WHERE id = $1", sid,
            )
        await self._emit_session(session, TABLE_CART_UPDATED, {"session_id": sid, "action": "item_removed"})
        return {"status": "removed" if "DELETE 1" in deleted else "not_found"}

    async def clear_cart(self, session_token: str) -> dict:
        session = await self._get_valid_session(session_token)
        sid = session["id"]
        async with get_connection() as conn:
            await conn.execute("DELETE FROM table_session_carts WHERE session_id = $1", sid)
            await conn.execute("UPDATE dine_in_sessions SET last_activity_at = now() WHERE id = $1", sid)
        await self._emit_session(session, TABLE_CART_UPDATED, {"session_id": sid, "action": "cart_cleared"})
        return {"status": "cleared"}

    async def get_cart(self, session_token: str) -> dict:
        session = await self._get_valid_session(session_token)
        sid = session["id"]
        await self._touch_activity(sid)
        async with get_connection() as conn:
            items = await conn.fetch(
                "SELECT * FROM table_session_carts WHERE session_id = $1 ORDER BY created_at",
                sid,
            )
        return {"session_id": sid, "cart": [dict(i) for i in items]}

    # ══════════════════════════════════════════════════════════
    # 3. ORDER PLACEMENT (Session-Scoped)
    # ══════════════════════════════════════════════════════════

    async def place_order(
        self,
        session_token: str,
        device_id: Optional[str] = None,
        notes: Optional[str] = None,
        customer_name: Optional[str] = None,
        customer_phone: Optional[str] = None,
        payment_method: str = "cash",
        request_id: Optional[str] = None,
    ) -> dict:
        """
        Place order from cart.
        - If session has ACTIVE order → append items to it
        - If no ACTIVE order → create new order
        - Idempotent via request_id
        """
        session = await self._get_valid_session(session_token)
        sid = session["id"]
        owner_id = session["user_id"]
        restaurant_id = str(session["restaurant_id"]) if session.get("restaurant_id") else None

        if request_id:
            cached = await self._check_idempotency(request_id, sid)
            if cached is not None:
                return cached

        try:
            async with DistributedLock(f"dinein_order:{sid}", timeout=15):
                async with get_serializable_transaction() as conn:
                    # ── Get cart ──
                    cart = await conn.fetch(
                        "SELECT * FROM table_session_carts WHERE session_id = $1",
                        sid,
                    )
                    if not cart:
                        raise ValidationError("Cart is empty")

                    # ── Get table info ──
                    table = await conn.fetchrow(
                        "SELECT id, table_number FROM restaurant_tables WHERE id = $1",
                        str(session["table_id"]),
                    )
                    table_number = table["table_number"] if table else None
                    table_id = str(table["id"]) if table else None

                    # ── Server-side price calc ──
                    subtotal = Decimal("0")
                    order_items_data = []
                    for ci in cart:
                        line_total = Decimal(str(ci["total_price"]))
                        subtotal += line_total
                        order_items_data.append({
                            "item_id": ci["item_id"],
                            "variant_id": ci.get("variant_id"),
                            "item_name": ci["item_name"],
                            "quantity": ci["quantity"],
                            "unit_price": float(ci["unit_price"]),
                            "total_price": float(ci["total_price"]),
                            "addons": ci.get("addons") or [],
                            "notes": ci.get("notes"),
                        })

                    # Tax
                    tax_row = await conn.fetchrow(
                        "SELECT tax_percentage FROM restaurant_settings WHERE user_id = $1",
                        owner_id,
                    )
                    tax_pct = Decimal(str(tax_row["tax_percentage"])) if tax_row and tax_row["tax_percentage"] else Decimal("0")
                    tax_amount = subtotal * tax_pct / 100
                    total_amount = subtotal + tax_amount

                    # ── Reuse ACTIVE order or create new ──
                    active_order_id = session.get("active_order_id")
                    is_append = False

                    if active_order_id:
                        # Check order still exists and is in editable state
                        existing_order = await conn.fetchrow(
                            "SELECT id, status FROM orders WHERE id = $1",
                            str(active_order_id),
                        )
                        if existing_order and existing_order["status"] in ("Queued", "Preparing"):
                            is_append = True
                            order_id = str(existing_order["id"])
                        else:
                            active_order_id = None

                    if not is_append:
                        # Create new order
                        order_id = str(uuid.uuid4())
                        await conn.execute(
                            """
                            INSERT INTO orders (
                                id, user_id, branch_id, restaurant_id, customer_id,
                                source, subtotal, tax_amount, discount_amount, total_amount,
                                status, table_number, delivery_phone,
                                notes, items, metadata
                            ) VALUES (
                                $1, $2, $3, $4, NULL, 'qr_table'::order_source,
                                $5, $6, 0, $7, 'Queued', $8, $9,
                                $10, '[]'::jsonb, $11::jsonb
                            )
                            """,
                            order_id, owner_id,
                            str(session.get("branch_id")) if session.get("branch_id") else None,
                            restaurant_id, float(subtotal), float(tax_amount), float(total_amount),
                            table_number, customer_phone, notes,
                            json.dumps({
                                "customer_name": customer_name,
                                "device_id": device_id,
                                "session_id": sid,
                                "payment_method": payment_method,
                            }),
                        )

                        # Link session → order
                        await conn.execute(
                            """
                            INSERT INTO session_orders (session_id, order_id, role)
                            VALUES ($1, $2, 'owner')
                            ON CONFLICT (session_id, order_id) DO NOTHING
                            """,
                            sid, order_id,
                        )

                        # Update session's active order
                        await conn.execute(
                            "UPDATE dine_in_sessions SET active_order_id = $1, last_activity_at = now() WHERE id = $2",
                            order_id, sid,
                        )
                    else:
                        # Append: update order totals
                        await conn.execute(
                            """
                            UPDATE orders
                            SET subtotal = subtotal + $1,
                                tax_amount = tax_amount + $2,
                                total_amount = total_amount + $3,
                                updated_at = now()
                            WHERE id = $4
                            """,
                            float(subtotal), float(tax_amount), float(total_amount),
                            order_id,
                        )

                    # ── Insert order_items ──
                    order_item_rows = []
                    for oi in order_items_data:
                        oi_row = await conn.fetchrow(
                            """
                            INSERT INTO order_items (
                                order_id, item_id, variant_id, item_name,
                                quantity, unit_price, total_price, addons, notes, user_id, session_id
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11)
                            RETURNING id
                            """,
                            order_id, oi["item_id"], oi.get("variant_id"), oi["item_name"],
                            oi["quantity"], oi["unit_price"], oi["total_price"],
                            json.dumps(oi.get("addons") or []), oi.get("notes"), owner_id, sid,
                        )
                        order_item_rows.append({
                            "order_item_id": oi_row["id"],
                            "item_name": oi["item_name"],
                            "quantity": oi["quantity"],
                        })

                    # ── Kitchen order (one per place-order action) ──
                    kitchen_order_id = str(uuid.uuid4())
                    await conn.execute(
                        """
                        INSERT INTO kitchen_orders (
                            id, order_id, restaurant_id, status, user_id,
                            priority, source, table_session_id, branch_id, created_at
                        ) VALUES (
                            $1, $2, $3, 'queued'::kitchen_status, $4,
                            0, 'qr_table', $5, $6, now()
                        )
                        """,
                        kitchen_order_id, order_id, restaurant_id, owner_id,
                        sid,
                        str(session.get("branch_id")) if session.get("branch_id") else None,
                    )

                    for oi_info in order_item_rows:
                        await conn.execute(
                            """
                            INSERT INTO kitchen_order_items (kitchen_order_id, order_item_id, status, item_name, quantity)
                            VALUES ($1, $2, 'queued'::kitchen_status, $3, $4)
                            """,
                            kitchen_order_id, oi_info["order_item_id"], oi_info["item_name"], oi_info["quantity"],
                        )

                    # ── Update table ──
                    if table_id:
                        await conn.execute(
                            "UPDATE restaurant_tables SET current_order_id = $1 WHERE id = $2",
                            order_id, table_id,
                        )

                    # ── Clear cart ──
                    await conn.execute("DELETE FROM table_session_carts WHERE session_id = $1", sid)

                    # Get order number
                    order_number = await conn.fetchval("SELECT order_number FROM orders WHERE id = $1", order_id)

                order_number = str(order_number) if order_number else order_id[:8]

                result = {
                    "order_id": order_id,
                    "order_number": order_number,
                    "kitchen_order_id": kitchen_order_id,
                    "total": float(total_amount),
                    "is_append": is_append,
                    "items_added": len(order_item_rows),
                    "status": "Queued",
                }

                if request_id:
                    await self._save_idempotency(request_id, sid, result)

                # ── Events: emit to session channel (not table!) ──
                await self._emit_to_order_sessions(
                    order_id, TABLE_ORDER_PLACED,
                    {
                        "order_id": order_id,
                        "order_number": order_number,
                        "session_id": sid,
                        "table_number": table_number,
                        "total": float(total_amount),
                        "is_append": is_append,
                    },
                    restaurant_id=restaurant_id,
                )

                await emit_and_publish(DomainEvent(
                    event_type=KITCHEN_ORDER_CREATED,
                    payload={
                        "kitchen_order_id": kitchen_order_id,
                        "order_id": order_id,
                        "order_number": order_number,
                        "session_id": sid,
                        "table_number": table_number,
                        "source": "qr_table",
                        "item_count": len(order_item_rows),
                    },
                    user_id=owner_id,
                    restaurant_id=restaurant_id,
                ))

                return result

        except LockError:
            raise LockAcquisitionError(f"dinein_order:{sid}")

    async def get_order_status(self, session_token: str) -> dict:
        """
        Get orders for THIS session only (strict isolation).
        Returns active order + history.
        """
        session = await self._get_valid_session(session_token)
        sid = session["id"]

        await self._touch_activity(sid)

        async with get_connection() as conn:
            # Get orders linked to this session
            orders = await conn.fetch(
                """
                SELECT o.id, o.order_number, o.status, o.total_amount, o.subtotal,
                       o.tax_amount, o.table_number, o.notes, o.metadata, o.created_at
                FROM orders o
                JOIN session_orders so ON so.order_id = o.id
                WHERE so.session_id = $1
                ORDER BY o.created_at DESC
                """,
                sid,
            )

            result = []
            for o in orders:
                oid = str(o["id"])
                kitchen_data = await self._get_kitchen_status(conn, oid)
                result.append({
                    "order_id": oid,
                    "order_number": o["order_number"],
                    "status": o["status"],
                    "total": float(o["total_amount"]),
                    "subtotal": float(o["subtotal"]),
                    "tax_amount": float(o["tax_amount"]),
                    "table_number": o["table_number"],
                    "created_at": o["created_at"].isoformat() if o["created_at"] else None,
                    **kitchen_data,
                })

        return {"session_id": sid, "orders": result}

    # ══════════════════════════════════════════════════════════
    # 4. ORDER MERGE (User-Controlled)
    # ══════════════════════════════════════════════════════════

    async def merge_sessions(
        self,
        source_session_token: str,
        target_session_token: str,
    ) -> dict:
        """
        Merge source session's order into target session's order.
        Both sessions must be on the same table and active.
        Post-merge: both sessions see the same order.
        """
        source = await self._get_valid_session(source_session_token)
        target = await self._get_valid_session(target_session_token)

        source_sid = source["id"]
        target_sid = target["id"]

        if source_sid == target_sid:
            raise ValidationError("Cannot merge session with itself")

        if str(source["table_id"]) != str(target["table_id"]):
            raise ValidationError("Sessions must be on the same table to merge")

        # Prevent circular merges
        if source.get("merged_into_session_id"):
            raise ConflictError("Source session is already merged")
        if target.get("merged_into_session_id"):
            raise ConflictError("Target session is already merged into another session")

        try:
            async with DistributedLock(f"merge:{source_sid}:{target_sid}", timeout=15):
                async with get_serializable_transaction() as conn:
                    source_order_id = source.get("active_order_id")
                    target_order_id = target.get("active_order_id")

                    # ── Ensure target has an order ──
                    if not target_order_id:
                        if not source_order_id:
                            raise ValidationError("Neither session has an active order to merge")
                        # Source has order, target doesn't → just link target to source's order
                        target_order_id = source_order_id
                        await conn.execute(
                            "UPDATE dine_in_sessions SET active_order_id = $1, last_activity_at = now() WHERE id = $2",
                            str(target_order_id), target_sid,
                        )

                    target_order_id_str = str(target_order_id)

                    # ── Transfer items from source order to target order ──
                    if source_order_id and str(source_order_id) != target_order_id_str:
                        source_order_id_str = str(source_order_id)

                        # Verify neither order is in terminal state
                        for oid, label in [(source_order_id_str, "source"), (target_order_id_str, "target")]:
                            order_row = await conn.fetchrow("SELECT status FROM orders WHERE id = $1", oid)
                            if order_row and order_row["status"] in ("Cancelled", "Rejected", "Served", "Delivered"):
                                raise ConflictError(f"Cannot merge: {label} order is {order_row['status']}")

                        # Move order_items
                        await conn.execute(
                            "UPDATE order_items SET order_id = $1 WHERE order_id = $2",
                            target_order_id_str, source_order_id_str,
                        )

                        # Move kitchen_order_items (via kitchen_orders)
                        await conn.execute(
                            "UPDATE kitchen_orders SET order_id = $1 WHERE order_id = $2",
                            target_order_id_str, source_order_id_str,
                        )

                        # Recalculate target order totals
                        totals = await conn.fetchrow(
                            """
                            SELECT COALESCE(SUM(total_price), 0) AS subtotal
                            FROM order_items WHERE order_id = $1
                            """,
                            target_order_id_str,
                        )
                        new_subtotal = Decimal(str(totals["subtotal"]))
                        tax_row = await conn.fetchrow(
                            "SELECT tax_percentage FROM restaurant_settings WHERE user_id = $1",
                            source["user_id"],
                        )
                        tax_pct = Decimal(str(tax_row["tax_percentage"])) if tax_row and tax_row["tax_percentage"] else Decimal("0")
                        new_tax = new_subtotal * tax_pct / 100
                        new_total = new_subtotal + new_tax

                        await conn.execute(
                            """
                            UPDATE orders SET subtotal = $1, tax_amount = $2, total_amount = $3,
                                   metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{merged}', 'true'::jsonb),
                                   updated_at = now()
                            WHERE id = $4
                            """,
                            float(new_subtotal), float(new_tax), float(new_total),
                            target_order_id_str,
                        )

                        # Mark source order as MERGED
                        await conn.execute(
                            "UPDATE orders SET status = 'Cancelled', notes = 'Merged into ' || $1, updated_at = now() WHERE id = $2",
                            target_order_id_str, source_order_id_str,
                        )

                    # ── Transfer cart items ──
                    await conn.execute(
                        "UPDATE table_session_carts SET session_id = $1 WHERE session_id = $2",
                        target_sid, source_sid,
                    )

                    # ── Link source session to target order ──
                    await conn.execute(
                        """
                        INSERT INTO session_orders (session_id, order_id, role)
                        VALUES ($1, $2, 'linked')
                        ON CONFLICT (session_id, order_id) DO UPDATE SET role = 'linked'
                        """,
                        source_sid, target_order_id_str,
                    )

                    # ── Link target session to target order (if not already) ──
                    await conn.execute(
                        """
                        INSERT INTO session_orders (session_id, order_id, role)
                        VALUES ($1, $2, 'owner')
                        ON CONFLICT (session_id, order_id) DO NOTHING
                        """,
                        target_sid, target_order_id_str,
                    )

                    # ── Update source session ──
                    await conn.execute(
                        """
                        UPDATE dine_in_sessions
                        SET merged_into_session_id = $1, active_order_id = $2, last_activity_at = now()
                        WHERE id = $3
                        """,
                        target_sid, target_order_id_str, source_sid,
                    )

                # ── Emit merge event to both sessions ──
                merge_payload = {
                    "source_session_id": source_sid,
                    "target_session_id": target_sid,
                    "order_id": target_order_id_str,
                }

                await emit_and_publish(DomainEvent(
                    event_type=SESSION_MERGED,
                    payload=merge_payload,
                    restaurant_id=str(source.get("restaurant_id")) if source.get("restaurant_id") else None,
                ))

                return {
                    "status": "merged",
                    "order_id": target_order_id_str,
                    "source_session_id": source_sid,
                    "target_session_id": target_sid,
                }

        except LockError:
            raise LockAcquisitionError(f"merge:{source_sid}:{target_sid}")

    # ══════════════════════════════════════════════════════════
    # 5. KITCHEN VIEW (Table-Grouped, NOT Session-Isolated)
    # ══════════════════════════════════════════════════════════

    async def get_kitchen_table_view(self, user_id: str, restaurant_id: str) -> list[dict]:
        """
        Kitchen display: orders grouped by table, with session breakdown.
        Kitchen does NOT follow session isolation — sees full table context.
        """
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    rt.id AS table_id, rt.table_number,
                    ds.id AS session_id, ds.device_id, ds.status AS session_status,
                    o.id AS order_id, o.order_number, o.status AS order_status,
                    o.total_amount, o.metadata,
                    ko.id AS kitchen_order_id, ko.status AS kitchen_status,
                    ko.created_at AS ko_created_at
                FROM dine_in_sessions ds
                JOIN restaurant_tables rt ON rt.id = ds.table_id
                LEFT JOIN session_orders so ON so.session_id = ds.id
                LEFT JOIN orders o ON o.id = so.order_id
                LEFT JOIN kitchen_orders ko ON ko.order_id = o.id
                WHERE (rt.user_id = $1 OR rt.restaurant_id = $2)
                  AND ds.status = 'active'
                ORDER BY rt.table_number, ds.created_at, ko.created_at
                """,
                user_id, restaurant_id,
            )

            # Group by table
            tables: dict[str, dict] = {}
            for r in rows:
                tid = str(r["table_id"])
                if tid not in tables:
                    tables[tid] = {
                        "table_id": tid,
                        "table_number": r["table_number"],
                        "sessions": {},
                    }
                sid = str(r["session_id"])
                if sid not in tables[tid]["sessions"]:
                    tables[tid]["sessions"][sid] = {
                        "session_id": sid,
                        "device_id": r["device_id"],
                        "session_status": r["session_status"],
                        "orders": [],
                    }
                if r["order_id"]:
                    is_merged = False
                    if r["metadata"]:
                        meta = r["metadata"] if isinstance(r["metadata"], dict) else json.loads(r["metadata"])
                        is_merged = meta.get("merged", False)

                    tables[tid]["sessions"][sid]["orders"].append({
                        "order_id": str(r["order_id"]),
                        "order_number": r["order_number"],
                        "order_status": r["order_status"],
                        "total": float(r["total_amount"]) if r["total_amount"] else 0,
                        "kitchen_order_id": str(r["kitchen_order_id"]) if r["kitchen_order_id"] else None,
                        "kitchen_status": r["kitchen_status"],
                        "is_merged": is_merged,
                    })

            # Flatten sessions dict to list
            result = []
            for t in tables.values():
                t["sessions"] = list(t["sessions"].values())
                result.append(t)

            return result

    # ══════════════════════════════════════════════════════════
    # 6. MENU (Reuse existing pattern)
    # ══════════════════════════════════════════════════════════

    async def get_menu(self, restaurant_id: str) -> dict:
        """Return full menu for QR ordering — delegates to existing logic."""
        from app.services.table_service import TableSessionService
        return await TableSessionService().qr_menu(restaurant_id)

    # ══════════════════════════════════════════════════════════
    # 7. CALL WAITER
    # ══════════════════════════════════════════════════════════

    async def call_waiter(self, session_token: str, request_type: str = "assistance") -> dict:
        session = await self._get_valid_session(session_token)
        sid = session["id"]

        async with get_connection() as conn:
            table = await conn.fetchrow(
                "SELECT table_number FROM restaurant_tables WHERE id = $1",
                str(session["table_id"]),
            )

        await emit_and_publish(DomainEvent(
            event_type=TABLE_CALL_WAITER,
            payload={
                "session_id": sid,
                "table_id": str(session["table_id"]),
                "table_number": table["table_number"] if table else "?",
                "request_type": request_type,
            },
            user_id=session["user_id"],
            restaurant_id=str(session["restaurant_id"]) if session.get("restaurant_id") else None,
        ))

        return {"status": "sent", "request_type": request_type}

    # ══════════════════════════════════════════════════════════
    # PRIVATE HELPERS
    # ══════════════════════════════════════════════════════════

    async def _get_valid_session(self, session_token: str) -> dict:
        """Validate session token, check expiry and status."""
        async with get_connection() as conn:
            session = await conn.fetchrow(
                """
                SELECT * FROM dine_in_sessions
                WHERE session_token = $1
                """,
                session_token,
            )
        if not session:
            raise NotFoundError("Session", "invalid token")

        if session["status"] != "active":
            if session["status"] == "expired":
                raise ValidationError("Session has expired. Please scan QR again.")
            elif session["status"] == "completed":
                raise ValidationError("Session is closed.")
            elif session["status"] == "merged":
                # Redirect to merged session — the token is still usable
                # but the active_order_id points to the merged order
                return dict(session)
            else:
                raise ValidationError(f"Session is {session['status']}")

        if session["expires_at"] and session["expires_at"] < datetime.now(timezone.utc):
            # Auto-expire
            async with get_connection() as conn:
                await conn.execute(
                    "UPDATE dine_in_sessions SET status = 'expired', ended_at = now() WHERE id = $1",
                    str(session["id"]),
                )
            raise ValidationError("Session has expired. Please scan QR again.")

        return dict(session)

    async def _try_restore_session(self, session_token: str) -> Optional[dict]:
        """Try to restore an existing session from client-provided token."""
        async with get_connection() as conn:
            session = await conn.fetchrow(
                """
                SELECT ds.*, rt.table_number, rt.capacity,
                       r.id AS rest_id, r.name AS rest_name, r.logo_url, r.phone, r.address, r.city
                FROM dine_in_sessions ds
                JOIN restaurant_tables rt ON rt.id = ds.table_id
                LEFT JOIN restaurants r ON r.id = ds.restaurant_id
                WHERE ds.session_token = $1 AND ds.status = 'active'
                """,
                session_token,
            )
        if not session:
            return None

        if session["expires_at"] and session["expires_at"] < datetime.now(timezone.utc):
            return None

        return {
            "id": str(session["id"]),
            "session_token": session["session_token"],
            "table_id": str(session["table_id"]),
            "restaurant_id": str(session["restaurant_id"]) if session["restaurant_id"] else None,
            "active_order_id": str(session["active_order_id"]) if session["active_order_id"] else None,
            "table": {
                "id": str(session["table_id"]),
                "table_number": session["table_number"],
                "capacity": session["capacity"],
            },
            "restaurant": {
                "id": str(session["rest_id"]) if session["rest_id"] else None,
                "name": session["rest_name"],
                "logo_url": session.get("logo_url"),
                "phone": session.get("phone"),
                "address": session.get("address"),
                "city": session.get("city"),
            } if session["rest_id"] else {},
        }

    async def _touch_activity(self, session_id: str):
        """Update last_activity_at for session."""
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE dine_in_sessions SET last_activity_at = now() WHERE id = $1",
                session_id,
            )

    async def _get_order_snapshot(self, conn, order_id: str) -> Optional[dict]:
        """Get full order with items for session state recovery."""
        order = await conn.fetchrow(
            """
            SELECT id, order_number, status, total_amount, subtotal, tax_amount,
                   table_number, notes, created_at
            FROM orders WHERE id = $1
            """,
            order_id,
        )
        if not order:
            return None

        items = await conn.fetch(
            "SELECT id, item_name, quantity, unit_price, total_price, session_id FROM order_items WHERE order_id = $1",
            order_id,
        )

        return {
            "order_id": str(order["id"]),
            "order_number": order["order_number"],
            "status": order["status"],
            "total": float(order["total_amount"]),
            "subtotal": float(order["subtotal"]),
            "tax_amount": float(order["tax_amount"]),
            "items": [dict(i) for i in items],
        }

    async def _get_kitchen_status(self, conn, order_id: str) -> dict:
        """Get kitchen status for an order."""
        kos = await conn.fetch(
            "SELECT id, status, started_at, ready_at, served_at, created_at FROM kitchen_orders WHERE order_id = $1 ORDER BY created_at",
            order_id,
        )

        kitchen_items = []
        overall_status = "pending"
        estimated_mins = None

        for ko in kos:
            items = await conn.fetch(
                """
                SELECT koi.item_name, oi.quantity, koi.status, koi.started_at, koi.ready_at
                FROM kitchen_order_items koi
                JOIN order_items oi ON oi.id = koi.order_item_id
                WHERE koi.kitchen_order_id = $1
                """,
                str(ko["id"]),
            )
            kitchen_items.extend([{
                "item_name": ki["item_name"],
                "quantity": ki["quantity"],
                "status": ki["status"],
            } for ki in items])

            # Use latest kitchen order status as overall
            overall_status = ko["status"]
            if ko["status"] == "queued":
                estimated_mins = 15
            elif ko["status"] == "preparing" and ko["started_at"]:
                elapsed = (datetime.now(timezone.utc) - ko["started_at"]).total_seconds() / 60
                estimated_mins = max(1, 10 - int(elapsed))

        return {
            "kitchen_status": overall_status,
            "estimated_mins": estimated_mins,
            "kitchen_items": kitchen_items,
        }

    async def _check_idempotency(self, request_id: str, session_id: str) -> Optional[dict]:
        """Check if request_id was already processed."""
        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT result FROM idempotency_keys WHERE key = $1 AND session_id = $2",
                request_id, session_id,
            )
        if row and row["result"]:
            return json.loads(row["result"]) if isinstance(row["result"], str) else row["result"]
        return None

    async def _save_idempotency(self, request_id: str, session_id: str, result: dict):
        """Save idempotency result."""
        async with get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO idempotency_keys (key, session_id, result)
                VALUES ($1, $2, $3::jsonb)
                ON CONFLICT (key) DO NOTHING
                """,
                request_id, session_id, json.dumps(result),
            )

    async def _emit_session(self, session: dict, event_type: str, payload: dict):
        """Emit event scoped to session channel (not table!)."""
        payload["session_id"] = session["id"]
        await emit_and_publish(DomainEvent(
            event_type=event_type,
            payload=payload,
            restaurant_id=str(session["restaurant_id"]) if session.get("restaurant_id") else None,
        ))

    async def _emit_to_order_sessions(
        self, order_id: str, event_type: str, payload: dict,
        restaurant_id: Optional[str] = None,
    ):
        """Emit event to ALL sessions linked to this order (for post-merge broadcasting)."""
        async with get_connection() as conn:
            session_ids = await conn.fetch(
                "SELECT session_id FROM session_orders WHERE order_id = $1",
                order_id,
            )

        payload["linked_session_ids"] = [str(r["session_id"]) for r in session_ids]
        await emit_and_publish(DomainEvent(
            event_type=event_type,
            payload=payload,
            restaurant_id=restaurant_id,
        ))
