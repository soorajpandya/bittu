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
BILL_UPDATED = "dinein.bill_updated"


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
        - Otherwise, reuse active session for the table or create one
        - Strict rule: one active session per table
        """
        # ── Try to restore existing session from client token ──
        if client_session_token:
            restored = await self._try_restore_session(client_session_token)
            if restored:
                await self._ensure_session_user(restored["id"], "qr", device_id=device_id)
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
                                    AND (rt.restaurant_id::text = $2::text OR rt.user_id::text = $2::text)
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

        # Reuse active session for this table if present.
        is_new = True
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                """
                SELECT id, session_token, active_order_id
                FROM dine_in_sessions
                WHERE table_id = $1
                  AND status = 'active'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                table_id,
            )

            if existing:
                session_id = str(existing["id"])
                session_token = existing["session_token"]
                is_new = False
                await conn.execute(
                    """
                    UPDATE dine_in_sessions
                    SET device_id = COALESCE($1, device_id),
                        last_activity_at = now()
                    WHERE id = $2
                    """,
                    device_id,
                    session_id,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO dine_in_sessions (
                        id, table_id, restaurant_id, user_id, branch_id,
                        session_token, device_id, guest_count, status,
                        last_activity_at, expires_at, started_at,
                        created_by, total_amount, paid_amount, remaining_amount,
                        active_users_count
                    ) VALUES ($1, $2, $3, $4, NULL, $5, $6, 1, 'active', $7, $8, $7, 'qr', 0, 0, 0, 0)
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

        await self._ensure_session_user(session_id, "qr", device_id=device_id)

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
            event_type=SESSION_CREATED if is_new else SESSION_RESTORED,
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
            "is_new": is_new,
            "active_order_id": str(existing["active_order_id"]) if not is_new and existing and existing.get("active_order_id") else None,
        }

    async def get_session_state(self, session_token: str) -> dict:
        """
        Get full session state including active order snapshot.
        Used on reconnect / page refresh.
        """
        session = await self._get_valid_session(session_token)
        sid = str(session["id"])

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
        sid = str(session["id"])
        session_user_id = await self._ensure_session_user(sid, "qr", device_id=device_id)

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
        sid = str(session["id"])
        session_user_id = await self._ensure_session_user(sid, "qr", device_id=device_id)

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
                                                    AND COALESCE(variant_id::text, '') = COALESCE($3::text, '')
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
                            notes, session_user_id, request_id,
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
        sid = str(session["id"])
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
                            SELECT $1, $2, 'owner'
                            WHERE NOT EXISTS (
                                SELECT 1 FROM session_orders WHERE session_id = $1 AND order_id = $2
                            )
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
                    has_order_items_session_id = await conn.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_name = 'order_items'
                              AND column_name = 'session_id'
                        )
                        """
                    )

                    order_item_rows = []
                    for oi in order_items_data:
                        if has_order_items_session_id:
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
                        else:
                            oi_row = await conn.fetchrow(
                                """
                                INSERT INTO order_items (
                                    order_id, item_id, variant_id, item_name,
                                    quantity, unit_price, total_price, addons, notes, user_id
                                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
                                RETURNING id
                                """,
                                order_id, oi["item_id"], oi.get("variant_id"), oi["item_name"],
                                oi["quantity"], oi["unit_price"], oi["total_price"],
                                json.dumps(oi.get("addons") or []), oi.get("notes"), owner_id,
                            )
                        order_item_rows.append({
                            "order_item_id": oi_row["id"],
                            "item_name": oi["item_name"],
                            "quantity": oi["quantity"],
                        })

                    # ── Kitchen order (one per place-order action) ──
                    kitchen_order_id = str(uuid.uuid4())
                    kitchen_cols_rows = await conn.fetch(
                        "SELECT column_name FROM information_schema.columns WHERE table_name = 'kitchen_orders'"
                    )
                    kitchen_cols = {r["column_name"] for r in kitchen_cols_rows}

                    ko_insert_cols = ["id", "order_id", "restaurant_id", "status", "user_id"]
                    ko_insert_vals = ["$1", "$2", "$3", "'queued'::kitchen_status", "$4"]
                    ko_params = [kitchen_order_id, order_id, restaurant_id, owner_id]
                    next_param = 5

                    if "priority" in kitchen_cols:
                        ko_insert_cols.append("priority")
                        ko_insert_vals.append("0")
                    if "source" in kitchen_cols:
                        ko_insert_cols.append("source")
                        ko_insert_vals.append("'qr_table'")
                    if "table_session_id" in kitchen_cols:
                        ko_insert_cols.append("table_session_id")
                        ko_insert_vals.append(f"${next_param}")
                        ko_params.append(sid)
                        next_param += 1
                    if "branch_id" in kitchen_cols:
                        ko_insert_cols.append("branch_id")
                        ko_insert_vals.append(f"${next_param}")
                        ko_params.append(str(session.get("branch_id")) if session.get("branch_id") else None)
                        next_param += 1
                    if "created_at" in kitchen_cols:
                        ko_insert_cols.append("created_at")
                        ko_insert_vals.append("now()")

                    await conn.execute(
                        f"INSERT INTO kitchen_orders ({', '.join(ko_insert_cols)}) VALUES ({', '.join(ko_insert_vals)})",
                        *ko_params,
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

                    # Get order number (works for schemas without orders.order_number column)
                    order_number = await conn.fetchval(
                        "SELECT COALESCE(metadata->>'order_number', LEFT(id::text, 8)) FROM orders WHERE id = $1",
                        order_id,
                    )

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
                  SELECT o.id,
                      COALESCE(o.metadata->>'order_number', LEFT(o.id::text, 8)) AS order_number,
                      o.status, o.total_amount, o.subtotal,
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
                    o.id AS order_id,
                    COALESCE(o.metadata->>'order_number', LEFT(o.id::text, 8)) AS order_number,
                    o.status AS order_status,
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
    # 8. BILLING / SPLIT / PAYMENTS
    # ══════════════════════════════════════════════════════════

    async def get_session_bill(self, session_id: str) -> dict:
        """Return aggregated session bill from all linked session orders."""
        async with get_connection() as conn:
            session = await conn.fetchrow(
                """
                SELECT id, table_id, restaurant_id, status, total_amount, paid_amount, remaining_amount
                FROM dine_in_sessions
                WHERE id = $1
                """,
                session_id,
            )
            if not session:
                raise NotFoundError("Session", session_id)

            orders = await conn.fetch(
                """
                  SELECT o.id,
                      COALESCE(o.metadata->>'order_number', LEFT(o.id::text, 8)) AS order_number,
                      o.status, o.subtotal, o.tax_amount,
                       o.discount_amount, o.total_amount, o.created_at
                FROM orders o
                JOIN session_orders so ON so.order_id = o.id
                WHERE so.session_id = $1
                ORDER BY o.created_at
                """,
                session_id,
            )

            order_ids = [str(o["id"]) for o in orders]
            grouped_items = []
            if order_ids:
                grouped_rows = await conn.fetch(
                    """
                    SELECT
                        oi.item_id,
                        oi.item_name,
                        SUM(oi.quantity) AS quantity,
                        SUM(oi.total_price) AS total_price
                    FROM order_items oi
                    WHERE oi.order_id = ANY($1::uuid[])
                    GROUP BY oi.item_id, oi.item_name
                    ORDER BY oi.item_name
                    """,
                    order_ids,
                )
                grouped_items = [
                    {
                        "item_id": r["item_id"],
                        "item_name": r["item_name"],
                        "quantity": int(r["quantity"] or 0),
                        "total_price": float(r["total_price"] or 0),
                    }
                    for r in grouped_rows
                ]

            subtotal = sum(Decimal(str(o["subtotal"] or 0)) for o in orders)
            tax = sum(Decimal(str(o["tax_amount"] or 0)) for o in orders)
            discount = sum(Decimal(str(o["discount_amount"] or 0)) for o in orders)
            grand_total = sum(Decimal(str(o["total_amount"] or 0)) for o in orders)

            payment_rows = await conn.fetch(
                """
                SELECT id, amount, payment_method, transaction_ref, paid_by, notes, created_at
                FROM table_session_payments
                WHERE session_id = $1
                ORDER BY created_at
                """,
                session_id,
            )

            paid_total = sum(Decimal(str(p["amount"] or 0)) for p in payment_rows)
            remaining = grand_total - paid_total
            if remaining < 0:
                remaining = Decimal("0")

            await conn.execute(
                """
                UPDATE dine_in_sessions
                SET total_amount = $1,
                    paid_amount = $2,
                    remaining_amount = $3
                WHERE id = $4
                """,
                float(grand_total),
                float(paid_total),
                float(remaining),
                session_id,
            )

        return {
            "session_id": session_id,
            "status": session["status"],
            "orders": [
                {
                    "order_id": str(o["id"]),
                    "order_number": o["order_number"],
                    "status": o["status"],
                    "subtotal": float(o["subtotal"] or 0),
                    "tax": float(o["tax_amount"] or 0),
                    "discount": float(o["discount_amount"] or 0),
                    "total": float(o["total_amount"] or 0),
                    "created_at": o["created_at"].isoformat() if o["created_at"] else None,
                }
                for o in orders
            ],
            "grouped_items": grouped_items,
            "subtotal": float(subtotal),
            "tax": float(tax),
            "discount": float(discount),
            "grand_total": float(grand_total),
            "paid_total": float(paid_total),
            "remaining_amount": float(remaining),
            "payments": [
                {
                    "payment_id": str(p["id"]),
                    "amount": float(p["amount"]),
                    "payment_method": p["payment_method"],
                    "transaction_ref": p["transaction_ref"],
                    "paid_by": p["paid_by"],
                    "notes": p["notes"],
                    "created_at": p["created_at"].isoformat() if p["created_at"] else None,
                }
                for p in payment_rows
            ],
        }

    async def split_bill(
        self,
        session_id: str,
        split_type: str,
        parts: int = 1,
        item_splits: Optional[list[dict]] = None,
        user_splits: Optional[list[dict]] = None,
    ) -> dict:
        """Compute split bill suggestions without mutating order totals."""
        bill = await self.get_session_bill(session_id)
        grand_total = Decimal(str(bill["grand_total"]))
        split_type = (split_type or "").lower()

        if grand_total <= 0:
            raise ValidationError("Session has no payable amount")

        if split_type == "equal":
            if parts <= 0:
                raise ValidationError("parts must be greater than 0")
            per = (grand_total / Decimal(str(parts))).quantize(Decimal("0.01"))
            shares = [{"label": f"part_{idx + 1}", "amount": float(per)} for idx in range(parts)]
            delta = grand_total - (per * parts)
            if delta != 0 and shares:
                shares[0]["amount"] = float(Decimal(str(shares[0]["amount"])) + delta)
            return {"session_id": session_id, "split_type": "equal", "shares": shares}

        if split_type == "by_item":
            if not item_splits:
                raise ValidationError("item_splits required for by_item")

            item_total = Decimal("0")
            shares = []
            for idx, row in enumerate(item_splits):
                amount = Decimal(str(row.get("amount") or 0))
                if amount <= 0:
                    continue
                item_total += amount
                shares.append({
                    "label": row.get("label") or f"item_group_{idx + 1}",
                    "amount": float(amount),
                    "items": row.get("items") or [],
                })

            if item_total <= 0:
                raise ValidationError("At least one positive item split amount is required")
            if item_total > grand_total:
                raise ValidationError("Item split total cannot exceed grand total")

            if item_total < grand_total:
                shares.append({"label": "unallocated", "amount": float(grand_total - item_total), "items": []})

            return {"session_id": session_id, "split_type": "by_item", "shares": shares}

        if split_type == "by_user":
            if user_splits:
                user_total = Decimal("0")
                shares = []
                for idx, row in enumerate(user_splits):
                    amount = Decimal(str(row.get("amount") or 0))
                    if amount <= 0:
                        continue
                    user_total += amount
                    shares.append({
                        "label": row.get("name") or f"user_{idx + 1}",
                        "session_user_id": row.get("session_user_id"),
                        "amount": float(amount),
                    })
                if user_total <= 0:
                    raise ValidationError("At least one positive user split amount is required")
                if user_total > grand_total:
                    raise ValidationError("User split total cannot exceed grand total")
                if user_total < grand_total:
                    shares.append({"label": "unallocated", "amount": float(grand_total - user_total)})
                return {"session_id": session_id, "split_type": "by_user", "shares": shares}

            async with get_connection() as conn:
                users = await conn.fetch(
                    """
                    SELECT id, COALESCE(name, CONCAT('guest_', LEFT(id::text, 6))) AS name
                    FROM table_session_users
                    WHERE session_id = $1 AND is_active = true
                    ORDER BY joined_at
                    """,
                    session_id,
                )

            if not users:
                raise ValidationError("No active session users found")

            per = (grand_total / Decimal(str(len(users)))).quantize(Decimal("0.01"))
            shares = [
                {
                    "label": u["name"],
                    "session_user_id": str(u["id"]),
                    "amount": float(per),
                }
                for u in users
            ]
            delta = grand_total - (per * len(users))
            if delta != 0 and shares:
                shares[0]["amount"] = float(Decimal(str(shares[0]["amount"])) + delta)
            return {"session_id": session_id, "split_type": "by_user", "shares": shares}

        raise ValidationError("split_type must be one of: equal, by_item, by_user")

    async def record_session_payment(
        self,
        session_id: str,
        amount: float,
        payment_method: str,
        created_by: Optional[str] = None,
        transaction_ref: Optional[str] = None,
        paid_by: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> dict:
        """Record partial/full payment against a table session."""
        amt = Decimal(str(amount or 0))
        if amt <= 0:
            raise ValidationError("Payment amount must be greater than zero")

        async with get_serializable_transaction() as conn:
            session = await conn.fetchrow(
                "SELECT id, status, restaurant_id FROM dine_in_sessions WHERE id = $1",
                session_id,
            )
            if not session:
                raise NotFoundError("Session", session_id)
            if session["status"] != "active":
                raise ValidationError("Payments are only allowed for active sessions")

            bill = await self.get_session_bill(session_id)
            remaining = Decimal(str(bill["remaining_amount"]))
            if remaining <= 0:
                raise ValidationError("Session is already fully paid")
            if amt > remaining:
                raise ValidationError(f"Payment exceeds remaining amount ({remaining})")

            payment_id = await conn.fetchval(
                """
                INSERT INTO table_session_payments (
                    session_id, amount, payment_method, transaction_ref,
                    paid_by, notes, created_by
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
                """,
                session_id,
                float(amt),
                payment_method,
                transaction_ref,
                paid_by,
                notes,
                created_by,
            )

        updated_bill = await self.get_session_bill(session_id)
        await emit_and_publish(DomainEvent(
            event_type=BILL_UPDATED,
            payload={
                "session_id": session_id,
                "payment_id": str(payment_id),
                "payment_method": payment_method,
                "amount": float(amt),
                "remaining_amount": updated_bill["remaining_amount"],
            },
            restaurant_id=str(session["restaurant_id"]) if session and session.get("restaurant_id") else None,
        ))

        return {
            "payment_id": str(payment_id),
            "session_id": session_id,
            "amount": float(amt),
            "remaining_amount": updated_bill["remaining_amount"],
            "status": "recorded",
        }

    async def paid_and_vacate(self, session_id: str, closed_by: Optional[str] = None) -> dict:
        """Close session only when remaining amount is zero, then free the table."""
        bill = await self.get_session_bill(session_id)
        remaining = Decimal(str(bill["remaining_amount"]))
        if remaining > 0:
            raise ValidationError(f"Cannot close session with unpaid amount: {remaining}")

        async with get_serializable_transaction() as conn:
            session = await conn.fetchrow(
                "SELECT id, table_id, restaurant_id, status FROM dine_in_sessions WHERE id = $1",
                session_id,
            )
            if not session:
                raise NotFoundError("Session", session_id)
            if session["status"] != "active":
                raise ValidationError(f"Session is already {session['status']}")

            await conn.execute(
                """
                UPDATE dine_in_sessions
                SET status = 'closed',
                    ended_at = now(),
                    active_users_count = 0,
                    last_activity_at = now()
                WHERE id = $1
                """,
                session_id,
            )

            await conn.execute(
                """
                UPDATE table_session_users
                SET is_active = false
                WHERE session_id = $1
                """,
                session_id,
            )

            await conn.execute(
                """
                UPDATE restaurant_tables
                SET status = 'blank',
                    is_occupied = false,
                    occupied_since = NULL,
                    session_token = NULL,
                    current_order_id = NULL
                WHERE id = $1
                """,
                str(session["table_id"]),
            )

        await emit_and_publish(DomainEvent(
            event_type=SESSION_CLOSED,
            payload={
                "session_id": session_id,
                "table_id": str(session["table_id"]),
                "closed_by": closed_by,
                "remaining_amount": 0,
            },
            restaurant_id=str(session["restaurant_id"]) if session.get("restaurant_id") else None,
        ))

        return {
            "status": "closed",
            "session_id": session_id,
            "table_id": str(session["table_id"]),
            "remaining_amount": 0.0,
        }

    # ══════════════════════════════════════════════════════════
    # PRIVATE HELPERS
    # ══════════════════════════════════════════════════════════

    async def _ensure_session_user(
        self,
        session_id: str,
        user_type: str,
        name: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> str:
        """Upsert active session participant and refresh session active user count."""
        async with get_serializable_transaction() as conn:
            row = None
            if device_id:
                row = await conn.fetchrow(
                    """
                    SELECT id FROM table_session_users
                    WHERE session_id = $1 AND device_id = $2
                    LIMIT 1
                    """,
                    session_id,
                    device_id,
                )

            if row:
                await conn.execute(
                    """
                    UPDATE table_session_users
                    SET is_active = true,
                        joined_at = COALESCE(joined_at, now()),
                        name = COALESCE($1, name)
                    WHERE id = $2
                    """,
                    name,
                    str(row["id"]),
                )
                session_user_id = str(row["id"])
            else:
                session_user_id = str(uuid.uuid4())
                await conn.execute(
                    """
                    INSERT INTO table_session_users (id, session_id, user_type, name, device_id, is_active)
                    VALUES ($1, $2, $3, $4, $5, true)
                    """,
                    session_user_id,
                    session_id,
                    user_type,
                    name,
                    device_id,
                )

            active_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM table_session_users
                WHERE session_id = $1 AND is_active = true
                """,
                session_id,
            )
            await conn.execute(
                "UPDATE dine_in_sessions SET active_users_count = $1 WHERE id = $2",
                int(active_count or 0),
                session_id,
            )

        return session_user_id

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
            elif session["status"] == "closed":
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
                 SELECT id,
                     COALESCE(metadata->>'order_number', LEFT(id::text, 8)) AS order_number,
                     status, total_amount, subtotal, tax_amount,
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
        session_id = str(session_id)
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
        session_id = str(session_id)
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
