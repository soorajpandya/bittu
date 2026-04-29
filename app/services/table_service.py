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
import asyncio
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
    TABLE_STATUS_CHANGED, TABLE_CALL_WAITER, KITCHEN_ORDER_CREATED,
)
from app.core.exceptions import (
    AppException, NotFoundError, ConflictError, ValidationError, LockAcquisitionError,
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
        branch_id: Optional[str] = None,
        customer_name: Optional[str] = None,
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

    async def join_session_by_table(
        self,
        table_id: str,
        device_id: str,
        device_name: Optional[str] = None,
    ) -> dict:
        """
        Join the currently active session for a table.
        Frontend-safe alias for cases where the client only knows table_id.
        """
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT session_token
                FROM table_sessions
                WHERE table_id = $1 AND is_active = true
                ORDER BY started_at DESC
                LIMIT 1
                """,
                table_id,
            )
            if not row or not row["session_token"]:
                raise NotFoundError("Session", "no active session for table")

            return await self.join_session(
                session_token=row["session_token"],
                device_id=device_id,
                device_name=device_name,
            )

    async def join_or_create_session_by_table(
        self,
        user: UserContext,
        table_id: str,
        device_id: str,
        device_name: Optional[str] = None,
    ) -> dict:
        """
        Admin/POS flow:
        - If an active session exists for the table, join it.
        - Otherwise, start a new session and join it.
        """
        async def _join_latest_active() -> dict:
            async with get_connection() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT session_token
                    FROM table_sessions
                    WHERE table_id = $1 AND is_active = true
                    ORDER BY started_at DESC
                    LIMIT 1
                    """,
                    table_id,
                )
                if not row or not row["session_token"]:
                    raise NotFoundError("Session", "no active session for table")
                return await self.join_session(
                    session_token=row["session_token"],
                    device_id=device_id,
                    device_name=device_name,
                )

        # Fast path: join existing session if present
        try:
            return await _join_latest_active()
        except NotFoundError:
            pass

        try:
            started = await self.start_session(
                user=user,
                table_id=table_id,
                branch_id=user.branch_id,
            )
            session_token = started.get("session_token")
            if not session_token:
                raise ValidationError("Failed to create session")
        except (ConflictError, LockAcquisitionError):
            # Another request is creating/created the session (race or lock contention).
            # Make this endpoint idempotent by fetching the active session and joining it.
            # (short delay helps if creator txn hasn't committed yet)
            await asyncio.sleep(0.15)
            return await _join_latest_active()
        except AppException as exc:
            # Catch-all idempotency for other 409 variants (e.g., table state transition conflicts).
            if exc.status_code == 409:
                await asyncio.sleep(0.15)
                return await _join_latest_active()
            raise

        return await self.join_session(
            session_token=session_token,
            device_id=device_id,
            device_name=device_name,
        )

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

    async def add_to_cart_admin(
        self,
        user: UserContext,
        session_id: str,
        items: list[dict],
    ) -> dict:
        """
        Admin/POS cart add.
        Accepts a table session UUID and a list of items:
          [{item_id, variant_id?, quantity, notes?}, ...]
        """
        owner_id = user.owner_id if user.is_branch_user else user.user_id
        actor = owner_id

        # Resolve to a dine-in session id (FK on table_session_carts points to dine_in_sessions)
        dinein_session_id = None
        async with get_connection() as conn:
            dinein = await conn.fetchrow(
                """
                SELECT id, table_id, restaurant_id, user_id, status
                FROM dine_in_sessions
                WHERE id = $1 AND user_id = $2 AND status = 'active'
                """,
                session_id,
                owner_id,
            )
            if dinein:
                dinein_session_id = str(dinein["id"])
            else:
                legacy = await conn.fetchrow(
                    """
                    SELECT id, table_id, restaurant_id, user_id, branch_id, session_token, guest_count, expires_at, is_active
                    FROM table_sessions
                    WHERE id = $1 AND user_id = $2 AND is_active = true
                    """,
                    session_id,
                    owner_id,
                )
                if not legacy:
                    raise NotFoundError("Session", session_id)

                # Reuse an active dine-in session for this table, or create one (compat bridge).
                existing_dinein = await conn.fetchrow(
                    """
                    SELECT id
                    FROM dine_in_sessions
                    WHERE table_id = $1 AND user_id = $2 AND status = 'active'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    str(legacy["table_id"]),
                    owner_id,
                )
                if existing_dinein:
                    dinein_session_id = str(existing_dinein["id"])
                else:
                    created = await conn.fetchrow(
                        """
                        INSERT INTO dine_in_sessions (
                            table_id, restaurant_id, user_id, branch_id,
                            session_token, guest_count, status, expires_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, 'active', $7)
                        RETURNING id
                        """,
                        str(legacy["table_id"]),
                        str(legacy["restaurant_id"]) if legacy["restaurant_id"] else None,
                        owner_id,
                        str(legacy["branch_id"]) if legacy["branch_id"] else None,
                        legacy["session_token"],
                        int(legacy["guest_count"] or 1),
                        legacy["expires_at"],
                    )
                    dinein_session_id = str(created["id"])

        if not items:
            raise ValidationError("items cannot be empty")

        inserted: list[dict] = []
        try:
            async with DistributedLock(f"cart:{dinein_session_id}", timeout=10):
                async with get_serializable_transaction() as conn:
                    for raw in items:
                        raw_item_id = raw.get("item_id")
                        if raw_item_id is None:
                            raise ValidationError("item_id is required")
                        try:
                            item_id_int = int(raw_item_id)
                        except Exception:
                            raise ValidationError("item_id must be an integer")

                        quantity = raw.get("quantity", 1)
                        if not isinstance(quantity, int) or quantity < 1:
                            raise ValidationError("quantity must be >= 1")

                        variant_id_raw = raw.get("variant_id")
                        variant_id_int = None
                        if variant_id_raw not in (None, ""):
                            try:
                                variant_id_int = int(variant_id_raw)
                            except Exception:
                                raise ValidationError("variant_id must be an integer")
                        notes = raw.get("notes")

                        item = await conn.fetchrow(
                            """
                            SELECT "Item_ID", "Item_Name", price, "Available_Status"
                            FROM items WHERE "Item_ID" = $1
                            """,
                            item_id_int,
                        )
                        if not item or not item["Available_Status"]:
                            raise ValidationError("Item is not available")

                        unit_price = float(item["price"])
                        item_name = item["Item_Name"]
                        variant_name = None

                        if variant_id_int is not None:
                            variant = await conn.fetchrow(
                                "SELECT name, price FROM item_variants WHERE id = $1 AND item_id = $2 AND is_active = true",
                                variant_id_int,
                                item_id_int,
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
                            cart_item_id,
                            dinein_session_id,
                            item_id_int,
                            variant_id_int,
                            item_name,
                            variant_name,
                            quantity,
                            unit_price,
                            total_price,
                            "[]",
                            "[]",
                            notes,
                            actor,
                        )
                        inserted.append(
                            {
                                "cart_item_id": cart_item_id,
                                "item_id": item_id_int,
                                "item_name": item_name,
                                "quantity": quantity,
                                "total_price": total_price,
                            }
                        )

        except LockError:
            raise LockAcquisitionError(f"cart:{dinein_session_id}")

        return {"items": inserted, "count": len(inserted)}

    async def get_cart(self, session_token: str = None, session_id: str = None, **kwargs) -> list[dict]:
        """Get all items in the session cart. Accepts session_token OR session_id."""
        if session_id:
            sid = session_id
        elif session_token:
            session = await self._get_active_session(session_token)
            sid = str(session["id"])
        else:
            raise ValidationError("session_token or session_id required")
        async with get_connection() as conn:
            items = await conn.fetch(
                "SELECT * FROM table_session_carts WHERE session_id = $1 ORDER BY created_at",
                sid,
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
        Otherwise, create a new session (4-hour expiry).
        """
        async with get_connection() as conn:
            # Look up table by id — the url_id might be restaurant_id OR user_id (owner_id)
            logger.info("qr_scan_lookup", table_id=table_id, restaurant_id=restaurant_id)
            table = await conn.fetchrow(
                """
                SELECT id, table_number, capacity, user_id, restaurant_id
                FROM restaurant_tables
                WHERE id = $1::uuid AND is_active = true
                  AND (restaurant_id = $2::uuid OR user_id = $2::uuid)
                """,
                table_id, restaurant_id,
            )
            if not table:
                # Fallback: look up by just table id
                logger.info("qr_scan_fallback", table_id=table_id)
                table = await conn.fetchrow(
                    """
                    SELECT id, table_number, capacity, user_id, restaurant_id
                    FROM restaurant_tables
                    WHERE id = $1::uuid AND is_active = true
                    """,
                    table_id,
                )
            if not table:
                # Last resort: check if table exists at all (even inactive)
                any_table = await conn.fetchrow(
                    "SELECT id, is_active, user_id, restaurant_id FROM restaurant_tables WHERE id = $1::uuid",
                    table_id,
                )
                if any_table:
                    logger.warning("qr_scan_table_inactive",
                        table_id=table_id, is_active=any_table["is_active"],
                        user_id=str(any_table["user_id"]), restaurant_id=str(any_table["restaurant_id"]) if any_table["restaurant_id"] else None,
                    )
                else:
                    # Check if the two IDs are swapped (table_id might be first segment)
                    swap_table = await conn.fetchrow(
                        "SELECT id, table_number, capacity, user_id, restaurant_id FROM restaurant_tables WHERE id = $1::uuid AND is_active = true",
                        restaurant_id,
                    )
                    if swap_table:
                        logger.warning("qr_scan_ids_swapped", actual_table_id=restaurant_id, passed_as_restaurant=table_id)
                        table = swap_table
                    else:
                        logger.warning("qr_scan_table_not_found", table_id=table_id, restaurant_id=restaurant_id)
            if not table:
                raise NotFoundError("Table", table_id)

            owner_id = str(table["user_id"])
            actual_restaurant_id = str(table["restaurant_id"]) if table["restaurant_id"] else None

            restaurant = await conn.fetchrow(
                "SELECT id, name, logo_url, phone, address, city FROM restaurants WHERE id = $1",
                actual_restaurant_id,
            ) if actual_restaurant_id else None

        async with get_connection() as conn:
            existing = await conn.fetchrow(
                "SELECT * FROM table_sessions WHERE table_id = $1 AND is_active = true",
                table_id,
            )

        if existing and existing["expires_at"] and existing["expires_at"] > datetime.now(timezone.utc):
            session_id = str(existing["id"])
            session_token = existing["session_token"]
            branch_id = str(existing["branch_id"]) if existing["branch_id"] else None
            async with get_connection() as conn:
                try:
                    await conn.execute(
                        """
                        INSERT INTO table_session_devices (session_id, device_id)
                        VALUES ($1, $2)
                        ON CONFLICT (session_id, device_id)
                        DO UPDATE SET last_seen = now(), is_active = true
                        """,
                        session_id, device_id,
                    )
                except Exception:
                    # Fallback if unique constraint doesn't exist
                    await conn.execute(
                        "INSERT INTO table_session_devices (session_id, device_id) VALUES ($1, $2)",
                        session_id, device_id,
                    )
        else:
            session_id = str(uuid.uuid4())
            session_token = secrets.token_urlsafe(32)
            now = datetime.now(timezone.utc)
            expires_at = now + timedelta(hours=4)
            branch_id = None

            async with get_serializable_transaction() as conn:
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
                    session_id, table_id, actual_restaurant_id,
                    owner_id, session_token, now, expires_at,
                )

                await conn.execute(
                    """
                    UPDATE restaurant_tables
                    SET status = 'running', is_occupied = true,
                        occupied_since = $1, session_token = $2
                    WHERE id = $3
                    """,
                    now, session_token, table_id,
                )

                try:
                    await conn.execute(
                        """
                        INSERT INTO table_session_devices (session_id, device_id)
                        VALUES ($1, $2)
                        ON CONFLICT (session_id, device_id)
                        DO UPDATE SET last_seen = now(), is_active = true
                        """,
                        session_id, device_id,
                    )
                except Exception:
                    await conn.execute(
                        "INSERT INTO table_session_devices (session_id, device_id) VALUES ($1, $2)",
                        session_id, device_id,
                    )

        return {
            "session_id": session_id,
            "session_token": session_token,
            "branch_id": branch_id,
            "restaurant_id": actual_restaurant_id,
            "restaurant": dict(restaurant) if restaurant else {},
            "table": {
                "id": str(table["id"]),
                "table_number": table["table_number"],
                "capacity": table["capacity"],
            },
        }

    # ── QR MENU ──

    async def qr_menu(self, restaurant_id: str) -> dict:
        """Return full menu for QR ordering: categories, items, variants, addons, extras, modifiers, combos."""
        async with get_connection() as conn:
            # Look up owner from restaurant — try as restaurant_id first, then as user_id
            rest = await conn.fetchrow(
                "SELECT id, owner_id FROM restaurants WHERE id = $1", restaurant_id,
            )
            if not rest:
                # Maybe the passed id is actually the owner_id
                rest = await conn.fetchrow(
                    "SELECT id, owner_id FROM restaurants WHERE owner_id = $1 LIMIT 1", restaurant_id,
                )
            if not rest:
                raise NotFoundError("Restaurant", restaurant_id)
            user_id = str(rest["owner_id"])

            cats = await conn.fetch(
                "SELECT id, name, slug, description, image_url, sort_order FROM categories WHERE user_id = $1 AND is_active = true ORDER BY sort_order",
                user_id,
            )

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

            variants = await conn.fetch(
                "SELECT id, item_id, name, price, sku FROM item_variants WHERE user_id = $1 AND is_active = true",
                user_id,
            )

            addons = await conn.fetch(
                "SELECT id, item_id, name, price FROM item_addons WHERE user_id = $1 AND is_active = true",
                user_id,
            )

            extras = await conn.fetch(
                "SELECT id, item_id, name, price FROM item_extras WHERE user_id = $1 AND is_active = true",
                user_id,
            )

            # Modifier groups with nested options (restaurant-level)
            groups = await conn.fetch(
                "SELECT id, name, is_required, min_selections, max_selections FROM modifier_groups WHERE user_id = $1",
                user_id,
            )
            group_map = {}
            for g in groups:
                gd = dict(g)
                gd["id"] = str(gd["id"])
                gd["modifierOptions"] = []
                group_map[gd["id"]] = gd

            if groups:
                group_ids = [str(g["id"]) for g in groups]
                options = await conn.fetch(
                    "SELECT id, group_id, name, price, is_active FROM modifier_options WHERE group_id = ANY($1::uuid[]) AND is_active = true",
                    group_ids,
                )
                for o in options:
                    gid = str(o["group_id"])
                    if gid in group_map:
                        group_map[gid]["modifierOptions"].append({
                            "id": str(o["id"]),
                            "name": o["name"],
                            "price": float(o["price"]) if o["price"] else 0,
                        })

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
            "modifierGroups": list(group_map.values()),
            "combos": [dict(r) for r in combos],
            "comboItems": [dict(r) for r in combo_items],
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
        import json

        session_token = data.get("session_token")
        action = data.get("action", "add")
        session = await self._get_active_session(session_token)
        session_id = str(session["id"])

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
                    addons = data.get("addons") or []
                    extras = data.get("extras") or []
                    async with get_connection() as conn:
                        row = await conn.fetchrow(
                            "SELECT unit_price FROM table_session_carts WHERE id = $1 AND session_id = $2",
                            cart_item_id, session_id,
                        )
                        if row:
                            base = float(row["unit_price"])
                            addon_total = sum(float(a.get("price", 0)) for a in addons)
                            extra_total = sum(float(e.get("price", 0)) for e in extras)
                            total_price = (base + addon_total + extra_total) * quantity
                            await conn.execute(
                                """
                                UPDATE table_session_carts
                                SET quantity = $1, total_price = $2,
                                    addons = $3::jsonb, extras = $4::jsonb
                                WHERE id = $5 AND session_id = $6
                                """,
                                quantity, total_price,
                                json.dumps(addons), json.dumps(extras),
                                cart_item_id, session_id,
                            )
                    return {"status": "updated"}

                # ── Default: add ──
                item_id = data.get("item_id")
                variant_id = data.get("variant_id")
                quantity = data.get("quantity", 1)
                addons = data.get("addons") or []
                extras = data.get("extras") or []
                notes = data.get("notes")
                added_by = data.get("device_id")
                client_item_name = data.get("item_name")
                client_unit_price = data.get("unit_price")

                async with get_serializable_transaction() as conn:
                    item = await conn.fetchrow(
                        'SELECT "Item_ID", "Item_Name", price, "Available_Status" FROM items WHERE "Item_ID" = $1',
                        item_id,
                    )
                    if not item or not item["Available_Status"]:
                        raise ValidationError("Item not available")

                    item_name = client_item_name or item["Item_Name"]
                    unit_price = float(client_unit_price) if client_unit_price is not None else float(item["price"])
                    variant_name = None

                    if variant_id:
                        variant = await conn.fetchrow(
                            "SELECT name, price FROM item_variants WHERE id = $1 AND item_id = $2 AND is_active = true",
                            variant_id, item_id,
                        )
                        if variant:
                            unit_price = float(variant["price"])
                            variant_name = variant["name"]

                    addon_total = sum(float(a.get("price", 0)) for a in addons)
                    extra_total = sum(float(e.get("price", 0)) for e in extras)
                    total_price = (unit_price + addon_total + extra_total) * quantity

                    cart_item_id = str(uuid.uuid4())
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
        """Convert cart into order + kitchen_orders + kitchen_order_items."""
        import json
        from decimal import Decimal

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
            cart = await conn.fetch(
                "SELECT * FROM table_session_carts WHERE session_id = $1",
                session_id,
            )
            if not cart:
                raise ValidationError("Cart is empty")

            table = await conn.fetchrow(
                "SELECT id, table_number FROM restaurant_tables WHERE id = $1",
                str(session["table_id"]),
            )
            table_number = table["table_number"] if table else None
            table_id = str(table["id"]) if table else None

            # Server-side price calculation
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

            order_id = str(uuid.uuid4())

            # INSERT order — order_number auto-generated by DB
            await conn.execute(
                """
                INSERT INTO orders (
                    id, user_id, branch_id, restaurant_id, customer_id,
                    source, subtotal, tax_amount, discount_amount, total_amount,
                    status, table_number, delivery_address, delivery_phone,
                    coupon_id, notes, items, metadata
                ) VALUES (
                    $1, $2, $3, $4, NULL, 'qr_table'::order_source,
                    $5, $6, 0, $7, 'Queued', $8, NULL, $9,
                    NULL, $10, $11::jsonb, $12::jsonb
                )
                """,
                order_id, owner_id,
                str(session.get("branch_id")) if session.get("branch_id") else None,
                restaurant_id, float(subtotal), float(tax_amount), float(total_amount),
                table_number, customer_phone, notes,
                json.dumps([]),
                json.dumps({
                    "customer_name": customer_name,
                    "device_id": device_id,
                    "session_id": session_id,
                    "payment_method": payment_method,
                }),
            )

            # INSERT order_items — RETURNING id for kitchen linkage
            order_item_rows = []
            for oi in order_items_data:
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
                })

            # INSERT kitchen_orders
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
                session_id,
                str(session.get("branch_id")) if session.get("branch_id") else None,
            )

            # INSERT kitchen_order_items
            for oi_info in order_item_rows:
                await conn.execute(
                    """
                    INSERT INTO kitchen_order_items (kitchen_order_id, order_item_id, status, item_name, quantity)
                    VALUES ($1, $2, 'queued'::kitchen_status, $3, 1)
                    """,
                    kitchen_order_id, oi_info["order_item_id"], oi_info["item_name"],
                )

            # UPDATE restaurant_tables.current_order_id
            if table_id:
                await conn.execute(
                    "UPDATE restaurant_tables SET current_order_id = $1 WHERE id = $2",
                    order_id, table_id,
                )

            # Clear cart
            await conn.execute(
                "DELETE FROM table_session_carts WHERE session_id = $1", session_id,
            )

            # Get DB-generated order_number
            order_number = await conn.fetchval(
                "SELECT order_number FROM orders WHERE id = $1", order_id,
            )

        order_number = str(order_number) if order_number else order_id[:8]

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

        await emit_and_publish(DomainEvent(
            event_type=KITCHEN_ORDER_CREATED,
            payload={
                "kitchen_order_id": kitchen_order_id,
                "order_id": order_id,
                "order_number": order_number,
                "session_id": session_id,
                "table_number": table_number,
                "source": "qr_table",
                "item_count": len(order_item_rows),
            },
            user_id=owner_id,
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

    async def qr_order_status(self, session_token: str) -> dict:
        """Get all orders for this session with kitchen status."""
        session = await self._get_active_session(session_token)
        session_id = str(session["id"])
        owner_id = str(session["user_id"])

        async with get_connection() as conn:
            orders = await conn.fetch(
                """
                SELECT id, order_number, status, total_amount, subtotal, tax_amount,
                       table_number, notes, metadata, created_at
                FROM orders
                WHERE user_id = $1 AND metadata->>'session_id' = $2
                ORDER BY created_at DESC
                """,
                owner_id, session_id,
            )

            result = []
            for o in orders:
                oid = str(o["id"])

                ko = await conn.fetchrow(
                    "SELECT id, status, started_at, ready_at, served_at FROM kitchen_orders WHERE order_id = $1",
                    oid,
                )

                kitchen_items = []
                if ko:
                    kitchen_items = await conn.fetch(
                        """
                        SELECT koi.item_name, oi.quantity, koi.status,
                               koi.started_at, koi.ready_at
                        FROM kitchen_order_items koi
                        JOIN order_items oi ON oi.id = koi.order_item_id
                        WHERE koi.kitchen_order_id = $1
                        """,
                        str(ko["id"]),
                    )

                kitchen_status = ko["status"] if ko else "pending"
                started_at = ko["started_at"] if ko else None
                ready_at = ko["ready_at"] if ko else None

                estimated_mins = None
                if kitchen_status == "queued":
                    estimated_mins = 15
                elif kitchen_status == "preparing" and started_at:
                    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds() / 60
                    estimated_mins = max(1, 10 - int(elapsed))

                result.append({
                    "order_id": oid,
                    "order_number": o["order_number"],
                    "status": o["status"],
                    "kitchen_status": kitchen_status,
                    "total": float(o["total_amount"]),
                    "subtotal": float(o["subtotal"]),
                    "tax_amount": float(o["tax_amount"]),
                    "table_number": o["table_number"],
                    "created_at": o["created_at"].isoformat() if o["created_at"] else None,
                    "estimated_mins": estimated_mins,
                    "started_at": started_at.isoformat() if started_at else None,
                    "ready_at": ready_at.isoformat() if ready_at else None,
                    "kitchen_items": [
                        {
                            "item_name": ki["item_name"],
                            "quantity": ki["quantity"],
                            "status": ki["status"],
                        }
                        for ki in kitchen_items
                    ],
                })

        return {"orders": result}

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

    # ── CALL WAITER (from QR customer) ──

    async def call_waiter(self, data: dict) -> dict:
        """Customer requests waiter assistance from QR interface."""
        session_token = data["session_token"]
        request_type = data.get("request_type", "assistance")  # assistance | bill | water

        session = await self._get_active_session(session_token)
        session_id = str(session["id"])
        restaurant_id = str(session["restaurant_id"]) if session["restaurant_id"] else None

        # Look up table number
        async with get_connection() as conn:
            table = await conn.fetchrow(
                "SELECT table_number FROM restaurant_tables WHERE id = $1",
                str(session["table_id"]),
            )

        table_number = table["table_number"] if table else "?"

        await emit_and_publish(DomainEvent(
            event_type=TABLE_CALL_WAITER,
            payload={
                "session_id": session_id,
                "table_id": str(session["table_id"]),
                "table_number": table_number,
                "request_type": request_type,
            },
            user_id=str(session["user_id"]),
            restaurant_id=restaurant_id,
            branch_id=str(session["branch_id"]) if session.get("branch_id") else None,
        ))

        return {"status": "sent", "request_type": request_type}

    # ── MARK PAID & VACATE (admin) ──

    async def mark_paid_and_vacate(
        self, user: UserContext, session_id: str, order_id: Optional[str] = None,
    ) -> dict:
        """Mark order as paid and end the table session in one action."""
        async with get_connection() as conn:
            session = await conn.fetchrow(
                "SELECT * FROM table_sessions WHERE id = $1 AND is_active = true",
                session_id,
            )
            if not session:
                raise NotFoundError("Session", session_id)

            # Mark order(s) as Served (dine-in terminal state)
            if order_id:
                await conn.execute(
                    "UPDATE orders SET status = 'Served', updated_at = now() WHERE id = $1",
                    order_id,
                )
            else:
                # Mark all orders for this session as Served
                owner_id = user.owner_id if user.is_branch_user else user.user_id
                await conn.execute(
                    """
                    UPDATE orders SET status = 'Served', updated_at = now()
                    WHERE user_id = $1 AND metadata->>'session_id' = $2
                      AND status NOT IN ('Cancelled', 'Rejected', 'Served', 'Delivered')
                    """,
                    owner_id, session_id,
                )

        # End the session (reuses existing logic)
        result = await self.end_session(user=user, session_id=session_id)

        await emit_and_publish(DomainEvent(
            event_type=TABLE_STATUS_CHANGED,
            payload={
                "session_id": session_id,
                "table_id": str(session["table_id"]),
                "action": "paid_and_vacated",
            },
            user_id=user.user_id,
            restaurant_id=user.restaurant_id,
            branch_id=user.branch_id,
        ))

        return {"status": "paid_and_vacated", **result}
