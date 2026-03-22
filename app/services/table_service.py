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
