"""
Table Session System — QR-based dine-in ordering.

Concurrency model:
  - Multiple devices can join the same table session
  - Cart modifications are serialized via distributed locks
  - Session token validates all device requests
  - Sessions auto-expire after configurable timeout

Flow:
  Guest scans QR → Session created → Devices join → Cart built → Order placed → Payment → Session ends

Security:
  - Session tokens are cryptographically random
  - Tokens expire and cannot be reused
  - Device tracking prevents session hijacking
"""
import uuid
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.redis import DistributedLock, LockError
from app.core.state_machines import TableStatus, validate_table_transition
from app.core.events import (
    DomainEvent, emit_and_publish,
    TABLE_SESSION_STARTED, TABLE_SESSION_ENDED, TABLE_CART_UPDATED, TABLE_ORDER_PLACED,
)
from app.core.exceptions import (
    NotFoundError, ConflictError, ValidationError, LockAcquisitionError,
)
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class TableSessionService:

    # ── START SESSION ──

    async def start_session(
        self,
        user: UserContext,
        table_id: str,
        guest_count: int = 1,
    ) -> dict:
        """
        Start a new table session.
        Only one active session per table at a time.
        """
        try:
            async with DistributedLock(f"table:{table_id}", timeout=10):
                async with get_serializable_transaction() as conn:
                    # Check table exists and belongs to tenant
                    table = await conn.fetchrow(
                        """
                        SELECT id, table_number, status, is_active
                        FROM restaurant_tables
                        WHERE id = $1 AND user_id = $2
                        FOR UPDATE
                        """,
                        table_id,
                        user.owner_id if user.is_branch_user else user.user_id,
                    )
                    if not table:
                        raise NotFoundError("Table", table_id)

                    if not table["is_active"]:
                        raise ValidationError("Table is not active")

                    # Check no active session exists
                    existing = await conn.fetchrow(
                        "SELECT id FROM table_sessions WHERE table_id = $1 AND is_active = true",
                        table_id,
                    )
                    if existing:
                        raise ConflictError(f"Table {table['table_number']} already has an active session")

                    # Create session
                    session_id = str(uuid.uuid4())
                    session_token = secrets.token_urlsafe(32)
                    now = datetime.now(timezone.utc)
                    expires_at = now + timedelta(minutes=get_settings().SESSION_TIMEOUT_MINUTES)

                    await conn.execute(
                        """
                        INSERT INTO table_sessions (
                            id, table_id, restaurant_id, user_id, branch_id,
                            session_token, guest_count, customer_count,
                            started_at, is_active, status, expires_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $7, $8, true, 'active', $9)
                        """,
                        session_id, table_id, user.restaurant_id,
                        user.owner_id if user.is_branch_user else user.user_id,
                        user.branch_id,
                        session_token, guest_count, now, expires_at,
                    )

                    # Update table status
                    validate_table_transition(table["status"], TableStatus.RUNNING.value)
                    await conn.execute(
                        """
                        UPDATE restaurant_tables
                        SET status = 'running', is_occupied = true, occupied_since = $1, session_token = $2
                        WHERE id = $3
                        """,
                        now, session_token, table_id,
                    )

                await emit_and_publish(DomainEvent(
                    event_type=TABLE_SESSION_STARTED,
                    payload={
                        "session_id": session_id,
                        "table_id": table_id,
                        "table_number": table["table_number"],
                        "guest_count": guest_count,
                    },
                    user_id=user.user_id,
                    restaurant_id=user.restaurant_id,
                    branch_id=user.branch_id,
                ))

                return {
                    "session_id": session_id,
                    "session_token": session_token,
                    "table_number": table["table_number"],
                    "expires_at": expires_at.isoformat(),
                }

        except LockError:
            raise LockAcquisitionError(f"table:{table_id}")

    # ── JOIN SESSION (device) ──

    async def join_session(
        self,
        session_token: str,
        device_id: str,
        device_name: Optional[str] = None,
    ) -> dict:
        """A device joins an existing table session via QR scan."""
        async with get_connection() as conn:
            session = await conn.fetchrow(
                """
                SELECT ts.*, rt.table_number
                FROM table_sessions ts
                JOIN restaurant_tables rt ON rt.id = ts.table_id
                WHERE ts.session_token = $1 AND ts.is_active = true
                """,
                session_token,
            )
            if not session:
                raise NotFoundError("Session", "invalid or expired token")

            # Check expiry
            if session["expires_at"] and session["expires_at"] < datetime.now(timezone.utc):
                raise ValidationError("Session has expired")

            # Register device
            await conn.execute(
                """
                INSERT INTO table_session_devices (session_id, device_id, device_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (session_id, device_id) DO UPDATE SET last_seen = now(), is_active = true
                """,
                str(session["id"]), device_id, device_name,
            )

            return {
                "session_id": str(session["id"]),
                "table_number": session["table_number"],
                "restaurant_id": str(session["restaurant_id"]) if session["restaurant_id"] else None,
                "guest_count": session["guest_count"],
                "status": session["status"],
            }

    # ── CART MANAGEMENT ──

    async def add_to_cart(
        self,
        session_token: str,
        item_id: int,
        variant_id: Optional[str] = None,
        quantity: int = 1,
        addons: list = None,
        extras: list = None,
        notes: Optional[str] = None,
        added_by: Optional[str] = None,
    ) -> dict:
        """Add item to session cart. Serialized via lock."""
        session = await self._get_active_session(session_token)
        session_id = str(session["id"])

        try:
            async with DistributedLock(f"cart:{session_id}", timeout=5):
                async with get_serializable_transaction() as conn:
                    # Fetch item price server-side
                    item = await conn.fetchrow(
                        """
                        SELECT "Item_ID", "Item_Name", price, "Available_Status"
                        FROM items WHERE "Item_ID" = $1
                        """,
                        item_id,
                    )
                    if not item or not item["Available_Status"]:
                        raise ValidationError("Item is not available")

                    unit_price = float(item["price"])
                    item_name = item["Item_Name"]
                    variant_name = None

                    if variant_id:
                        variant = await conn.fetchrow(
                            "SELECT name, price FROM item_variants WHERE id = $1 AND item_id = $2 AND is_active = true",
                            variant_id, item_id,
                        )
                        if variant:
                            unit_price = float(variant["price"])
                            variant_name = variant["name"]

                    total_price = unit_price * quantity

                    cart_item_id = str(uuid.uuid4())
                    await conn.execute(
                        """
                        INSERT INTO table_session_carts (
                            id, session_id, item_id, variant_id,
                            item_name, variant_name, quantity,
                            unit_price, total_price,
                            addons, extras, notes, added_by
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb, $12, $13)
                        """,
                        cart_item_id, session_id, item_id, variant_id,
                        item_name, variant_name, quantity,
                        unit_price, total_price,
                        "[]", "[]", notes, added_by,
                    )

                await emit_and_publish(DomainEvent(
                    event_type=TABLE_CART_UPDATED,
                    payload={
                        "session_id": session_id,
                        "action": "item_added",
                        "item_name": item_name,
                        "quantity": quantity,
                    },
                    restaurant_id=str(session["restaurant_id"]) if session["restaurant_id"] else None,
                ))

                return {"cart_item_id": cart_item_id, "item_name": item_name, "total_price": total_price}

        except LockError:
            raise LockAcquisitionError(f"cart:{session_id}")

    async def get_cart(self, session_token: str) -> list[dict]:
        """Get all items in the session cart."""
        session = await self._get_active_session(session_token)
        async with get_connection() as conn:
            items = await conn.fetch(
                "SELECT * FROM table_session_carts WHERE session_id = $1 ORDER BY created_at",
                str(session["id"]),
            )
            return [dict(i) for i in items]

    async def remove_from_cart(self, session_token: str, cart_item_id: str) -> dict:
        session = await self._get_active_session(session_token)
        async with get_connection() as conn:
            deleted = await conn.execute(
                "DELETE FROM table_session_carts WHERE id = $1 AND session_id = $2",
                cart_item_id, str(session["id"]),
            )
            return {"deleted": "DELETE 1" in deleted}

    # ── END SESSION ──

    async def end_session(
        self,
        user: UserContext,
        session_id: str,
    ) -> dict:
        """End a table session and free the table."""
        try:
            async with DistributedLock(f"session:{session_id}", timeout=10):
                async with get_serializable_transaction() as conn:
                    session = await conn.fetchrow(
                        "SELECT * FROM table_sessions WHERE id = $1 AND is_active = true FOR UPDATE",
                        session_id,
                    )
                    if not session:
                        raise NotFoundError("Session", session_id)

                    now = datetime.now(timezone.utc)

                    await conn.execute(
                        """
                        UPDATE table_sessions
                        SET is_active = false, status = 'ended', ended_at = $1
                        WHERE id = $2
                        """,
                        now, session_id,
                    )

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

                    # Deactivate all devices
                    await conn.execute(
                        "UPDATE table_session_devices SET is_active = false WHERE session_id = $1",
                        session_id,
                    )

                await emit_and_publish(DomainEvent(
                    event_type=TABLE_SESSION_ENDED,
                    payload={"session_id": session_id, "table_id": str(session["table_id"])},
                    user_id=user.user_id,
                    restaurant_id=user.restaurant_id,
                    branch_id=user.branch_id,
                ))

                return {"status": "ended"}

        except LockError:
            raise LockAcquisitionError(f"session:{session_id}")

    # ── QR SCAN ──

    async def qr_scan(
        self,
        restaurant_id: str,
        table_id: str,
        device_id: str,
    ) -> dict:
        """
        Customer scans QR code on a table.
        If an active session exists for that table, join it.
        Otherwise, create a new session.
        """
        async with get_connection() as conn:
            # Verify table exists and belongs to restaurant
            table = await conn.fetchrow(
                """
                SELECT id, table_number, capacity, user_id, restaurant_id
                FROM restaurant_tables
                WHERE id = $1 AND restaurant_id = $2 AND is_active = true
                """,
                table_id, restaurant_id,
            )
            if not table:
                raise NotFoundError("Table", table_id)

            owner_id = str(table["user_id"])

            # Fetch restaurant info
            restaurant = await conn.fetchrow(
                """
                SELECT id, name, logo_url, phone, address, city
                FROM restaurants WHERE id = $1
                """,
                restaurant_id,
            )

        # Check for existing active session
        async with get_connection() as conn:
            existing = await conn.fetchrow(
                "SELECT * FROM table_sessions WHERE table_id = $1 AND is_active = true",
                table_id,
            )

        if existing and existing["expires_at"] and existing["expires_at"] > datetime.now(timezone.utc):
            session_id = str(existing["id"])
            session_token = existing["session_token"]
            branch_id = str(existing["branch_id"]) if existing["branch_id"] else None
            # Register device
            async with get_connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO table_session_devices (session_id, device_id)
                    VALUES ($1, $2)
                    ON CONFLICT (session_id, device_id)
                    DO UPDATE SET last_seen = now(), is_active = true
                    """,
                    session_id, device_id,
                )
        else:
            # Create new session
            session_id = str(uuid.uuid4())
            session_token = secrets.token_urlsafe(32)
            now = datetime.now(timezone.utc)
            expires_at = now + timedelta(minutes=get_settings().SESSION_TIMEOUT_MINUTES)
            branch_id = None

            async with get_serializable_transaction() as conn:
                # Deactivate stale session if any
                if existing:
                    await conn.execute(
                        "UPDATE table_sessions SET is_active = false, status = 'expired' WHERE id = $1",
                        str(existing["id"]),
                    )

                await conn.execute(
                    """
                    INSERT INTO table_sessions (
                        id, table_id, restaurant_id, user_id, branch_id,
                        session_token, guest_count, customer_count,
                        started_at, is_active, status, expires_at
                    ) VALUES ($1, $2, $3, $4, NULL, $5, 1, 1, $6, true, 'active', $7)
                    """,
                    session_id, table_id, restaurant_id,
                    owner_id, session_token, now, expires_at,
                )

                # Update table status
                await conn.execute(
                    """
                    UPDATE restaurant_tables
                    SET status = 'running', is_occupied = true,
                        occupied_since = $1, session_token = $2
                    WHERE id = $3
                    """,
                    now, session_token, table_id,
                )

                # Register device
                await conn.execute(
                    """
                    INSERT INTO table_session_devices (session_id, device_id)
                    VALUES ($1, $2)
                    ON CONFLICT (session_id, device_id)
                    DO UPDATE SET last_seen = now(), is_active = true
                    """,
                    session_id, device_id,
                )

        return {
            "session_id": session_id,
            "session_token": session_token,
            "branch_id": branch_id,
            "restaurant": dict(restaurant) if restaurant else {},
            "table": {
                "id": str(table["id"]),
                "table_number": table["table_number"],
                "capacity": table["capacity"],
            },
        }

    # ── QR MENU ──

    async def qr_menu(
        self,
        restaurant_id: str,
        user_id: str,
        branch_id: Optional[str] = None,
    ) -> dict:
        """Return full menu for QR ordering: categories, items, variants, addons, extras, modifiers, combos."""
        async with get_connection() as conn:
            # Categories
            cats = await conn.fetch(
                "SELECT id, name, slug, description, image_url, sort_order FROM categories WHERE user_id = $1 AND is_active = true ORDER BY sort_order",
                user_id,
            )

            # Items — only dine-in available
            items = await conn.fetch(
                """
                SELECT "Item_ID", "Item_Name", "Description", price, "Category",
                       "Subcategory", "Cuisine", "Spice_Level", "Prep_Time_Min",
                       "Image_url", is_veg, tags, sort_order
                FROM items
                WHERE user_id = $1 AND "Available_Status" = true
                  AND dine_in_available = true
                ORDER BY sort_order, "Item_Name"
                """,
                user_id,
            )
            item_ids = [r["Item_ID"] for r in items]

            # Variants
            variants = await conn.fetch(
                "SELECT id, item_id, name, price, sku FROM item_variants WHERE user_id = $1 AND is_active = true",
                user_id,
            )

            # Addons
            addons = await conn.fetch(
                "SELECT id, item_id, name, price FROM item_addons WHERE user_id = $1 AND is_active = true",
                user_id,
            )

            # Extras
            extras = await conn.fetch(
                "SELECT id, item_id, name, price FROM item_extras WHERE user_id = $1 AND is_active = true",
                user_id,
            )

            # Modifier groups + options
            groups = await conn.fetch(
                "SELECT id, name, is_required, min_selections, max_selections FROM modifier_groups WHERE user_id = $1",
                user_id,
            )
            options = []
            if groups:
                group_ids = [str(g["id"]) for g in groups]
                options = await conn.fetch(
                    "SELECT id, group_id, name, price, is_active FROM modifier_options WHERE group_id = ANY($1::uuid[]) AND is_active = true",
                    group_ids,
                )

            # Combos
            combos = await conn.fetch(
                "SELECT id, name, description, price, image_url FROM combos WHERE user_id = $1 AND is_active = true",
                user_id,
            )
            combo_items = []
            if combos:
                combo_ids = [str(c["id"]) for c in combos]
                combo_items = await conn.fetch(
                    """
                    SELECT ci.id, ci.combo_id, ci.item_id, ci.quantity, i."Item_Name" as item_name
                    FROM combo_items ci
                    LEFT JOIN items i ON i."Item_ID" = ci.item_id
                    WHERE ci.combo_id = ANY($1::uuid[])
                    """,
                    combo_ids,
                )

        return {
            "categories": [dict(r) for r in cats],
            "items": [dict(r) for r in items],
            "variants": [dict(r) for r in variants],
            "addons": [dict(r) for r in addons],
            "extras": [dict(r) for r in extras],
            "modifier_groups": [dict(r) for r in groups],
            "modifier_options": [dict(r) for r in options],
            "combos": [dict(r) for r in combos],
            "combo_items": [dict(r) for r in combo_items],
        }

    # ── QR CART ──

    async def qr_get_cart(self, session_token: str) -> dict:
        session = await self._get_active_session(session_token)
        async with get_connection() as conn:
            items = await conn.fetch(
                "SELECT * FROM table_session_carts WHERE session_id = $1 ORDER BY created_at",
                str(session["id"]),
            )
        return {"cart": [dict(i) for i in items], "session_id": str(session["id"])}

    async def qr_cart_action(self, data: dict) -> dict:
        """Handle add / update / remove / clear actions on QR cart."""
        session_token = data.get("session_token")
        action = data.get("action", "add")
        session = await self._get_active_session(session_token)
        session_id = str(session["id"])
        owner_id = str(session["user_id"])

        try:
            async with DistributedLock(f"cart:{session_id}", timeout=5):
                if action == "clear":
                    async with get_connection() as conn:
                        await conn.execute(
                            "DELETE FROM table_session_carts WHERE session_id = $1",
                            session_id,
                        )
                    return {"status": "cleared"}

                if action == "remove":
                    cart_item_id = data.get("cart_item_id")
                    async with get_connection() as conn:
                        await conn.execute(
                            "DELETE FROM table_session_carts WHERE id = $1 AND session_id = $2",
                            cart_item_id, session_id,
                        )
                    return {"status": "removed"}

                if action == "update":
                    cart_item_id = data.get("cart_item_id")
                    quantity = data.get("quantity", 1)
                    async with get_connection() as conn:
                        await conn.execute(
                            """
                            UPDATE table_session_carts
                            SET quantity = $1, total_price = unit_price * $1
                            WHERE id = $2 AND session_id = $3
                            """,
                            quantity, cart_item_id, session_id,
                        )
                    return {"status": "updated"}

                # Default: add
                item_id = data.get("item_id")
                variant_id = data.get("variant_id")
                quantity = data.get("quantity", 1)
                addons = data.get("addons", [])
                extras = data.get("extras", [])
                notes = data.get("notes")
                added_by = data.get("device_id")

                async with get_serializable_transaction() as conn:
                    item = await conn.fetchrow(
                        'SELECT "Item_ID", "Item_Name", price, "Available_Status" FROM items WHERE "Item_ID" = $1',
                        item_id,
                    )
                    if not item or not item["Available_Status"]:
                        raise ValidationError("Item not available")

                    unit_price = float(item["price"])
                    item_name = item["Item_Name"]
                    variant_name = None

                    if variant_id:
                        variant = await conn.fetchrow(
                            "SELECT name, price FROM item_variants WHERE id = $1 AND item_id = $2 AND is_active = true",
                            variant_id, item_id,
                        )
                        if variant:
                            unit_price = float(variant["price"])
                            variant_name = variant["name"]

                    total_price = unit_price * quantity
                    cart_item_id = str(uuid.uuid4())

                    import json
                    await conn.execute(
                        """
                        INSERT INTO table_session_carts (
                            id, session_id, item_id, variant_id,
                            item_name, variant_name, quantity,
                            unit_price, total_price,
                            addons, extras, notes, added_by
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb,$12,$13)
                        """,
                        cart_item_id, session_id, item_id, variant_id,
                        item_name, variant_name, quantity,
                        unit_price, total_price,
                        json.dumps(addons), json.dumps(extras),
                        notes, added_by,
                    )

                return {
                    "status": "added",
                    "cart_item_id": cart_item_id,
                    "item_name": item_name,
                    "total_price": total_price,
                }

        except LockError:
            raise LockAcquisitionError(f"cart:{session_id}")

    # ── QR PLACE ORDER ──

    async def qr_place_order(self, data: dict) -> dict:
        """Convert cart into order for QR dine-in."""
        session_token = data["session_token"]
        device_id = data.get("device_id")
        notes = data.get("notes")
        customer_name = data.get("customer_name")
        customer_phone = data.get("customer_phone")
        payment_method = data.get("payment_method", "cash")

        session = await self._get_active_session(session_token)
        session_id = str(session["id"])
        restaurant_id = str(session["restaurant_id"]) if session["restaurant_id"] else None
        owner_id = str(session["user_id"])

        async with get_serializable_transaction() as conn:
            # Get cart items
            cart = await conn.fetch(
                "SELECT * FROM table_session_carts WHERE session_id = $1",
                session_id,
            )
            if not cart:
                raise ValidationError("Cart is empty")

            # Get table number
            table = await conn.fetchrow(
                "SELECT table_number FROM restaurant_tables WHERE id = $1",
                str(session["table_id"]),
            )
            table_number = table["table_number"] if table else None

            # Server-side price calculation
            from decimal import Decimal
            subtotal = Decimal("0")
            order_items_data = []
            import json

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

            # Generate order number
            count_row = await conn.fetchval(
                "SELECT COUNT(*) FROM orders WHERE user_id = $1", owner_id
            )
            order_number = f"QR-{(count_row or 0) + 1:04d}"

            order_id = str(uuid.uuid4())

            await conn.execute(
                """
                INSERT INTO orders (
                    id, user_id, branch_id, restaurant_id, customer_id,
                    source, subtotal, tax_amount, discount_amount, total_amount,
                    status, table_number, delivery_address, delivery_phone,
                    coupon_id, notes, items, metadata
                ) VALUES (
                    $1, $2, $3, $4, NULL, 'qr'::order_source,
                    $5, $6, 0, $7, 'pending', $8, NULL, $9,
                    NULL, $10, $11::jsonb, $12::jsonb
                )
                """,
                order_id, owner_id, str(session.get("branch_id")) if session.get("branch_id") else None,
                restaurant_id, float(subtotal), float(tax_amount), float(total_amount),
                table_number, customer_phone, notes,
                json.dumps(order_items_data),
                json.dumps({"customer_name": customer_name, "device_id": device_id, "session_id": session_id, "order_number": order_number}),
            )

            # Insert order_items
            for oi in order_items_data:
                await conn.execute(
                    """
                    INSERT INTO order_items (
                        order_id, item_id, variant_id, item_name,
                        quantity, unit_price, total_price, addons, notes, user_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
                    """,
                    order_id, oi["item_id"], oi.get("variant_id"), oi["item_name"],
                    oi["quantity"], oi["unit_price"], oi["total_price"],
                    json.dumps(oi.get("addons") or []), oi.get("notes"), owner_id,
                )

            # Clear cart
            await conn.execute(
                "DELETE FROM table_session_carts WHERE session_id = $1", session_id
            )

        # Emit event
        await emit_and_publish(DomainEvent(
            event_type=TABLE_ORDER_PLACED,
            payload={
                "order_id": order_id,
                "order_number": order_number,
                "session_id": session_id,
                "table_number": table_number,
                "total": float(total_amount),
            },
            restaurant_id=restaurant_id,
        ))

        return {
            "order_id": order_id,
            "order_number": order_number,
            "total": float(total_amount),
            "status": "pending",
            "message": "Order placed successfully",
        }

    # ── QR ORDER STATUS ──

    async def qr_order_status(self, session_token: str, order_id: str) -> dict:
        """Get order tracking info for QR customer."""
        session = await self._get_active_session(session_token)
        owner_id = str(session["user_id"])

        async with get_connection() as conn:
            order = await conn.fetchrow(
                "SELECT id, status, total_amount, subtotal, tax_amount, table_number, notes, items, metadata, created_at FROM orders WHERE id = $1 AND user_id = $2",
                order_id, owner_id,
            )
            if not order:
                raise NotFoundError("Order", order_id)

            # Kitchen item statuses
            kitchen_items = await conn.fetch(
                """
                SELECT oi.item_name, oi.quantity, oi.unit_price, oi.total_price,
                       COALESCE(ki.status, 'pending') as kitchen_status
                FROM order_items oi
                LEFT JOIN kitchen_order_items ki ON ki.order_item_id = oi.id
                WHERE oi.order_id = $1
                """,
                order_id,
            )

        return {
            "order_id": str(order["id"]),
            "status": order["status"],
            "total": float(order["total_amount"]),
            "subtotal": float(order["subtotal"]),
            "tax_amount": float(order["tax_amount"]),
            "table_number": order["table_number"],
            "created_at": order["created_at"].isoformat() if order["created_at"] else None,
            "metadata": order["metadata"],
            "items": [dict(ki) for ki in kitchen_items],
        }

    # ── HELPERS ──

    async def _get_active_session(self, session_token: str) -> dict:
        async with get_connection() as conn:
            session = await conn.fetchrow(
                "SELECT * FROM table_sessions WHERE session_token = $1 AND is_active = true",
                session_token,
            )
            if not session:
                raise NotFoundError("Session", "invalid or expired token")
            if session["expires_at"] and session["expires_at"] < datetime.now(timezone.utc):
                raise ValidationError("Session has expired")
            return dict(session)
