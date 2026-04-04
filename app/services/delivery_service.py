"""
Delivery & Tracking Service.

Handles:
  - Delivery assignment to partners
  - Real-time GPS tracking
  - Status transitions (unassigned → assigned → picked_up → delivered)
  - Partner availability management

Real-time:
  - Location updates published to Redis for WebSocket fan-out
  - Customer gets live tracking via order-specific channel
"""
from datetime import datetime, timezone
from typing import Optional

from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.redis import DistributedLock, LockError, get_redis
from app.core.state_machines import DeliveryStatus, validate_delivery_transition
from app.core.events import (
    DomainEvent, emit_and_publish,
    DELIVERY_ASSIGNED, DELIVERY_STATUS_CHANGED, DELIVERY_LOCATION_UPDATED,
)
from app.core.tenant import tenant_where_clause
from app.core.exceptions import NotFoundError, LockAcquisitionError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)


class DeliveryService:

    async def create_delivery(
        self,
        user: UserContext,
        order_id: str,
        delivery_address: str,
        delivery_phone: str,
        pickup_address: Optional[str] = None,
    ) -> dict:
        """Create a new delivery request for an order."""
        import uuid

        async with get_serializable_transaction() as conn:
            # Verify order
            order = await conn.fetchrow(
                "SELECT id, restaurant_id FROM orders WHERE id = $1 AND user_id = $2",
                order_id, user.owner_id if user.is_branch_user else user.user_id,
            )
            if not order:
                raise NotFoundError("Order", order_id)

            delivery_id = str(uuid.uuid4())
            await conn.execute(
                """
                INSERT INTO deliveries (
                    id, order_id, restaurant_id, status,
                    pickup_address, delivery_address, delivery_phone
                ) VALUES ($1, $2, $3, 'unassigned'::delivery_status, $4, $5, $6)
                """,
                delivery_id, order_id, str(order["restaurant_id"]),
                pickup_address, delivery_address, delivery_phone,
            )

        return {"delivery_id": delivery_id, "status": "unassigned"}

    async def assign_partner(
        self,
        user: UserContext,
        delivery_id: str,
        partner_id: str,
    ) -> dict:
        """Assign a delivery partner. Lock both delivery and partner."""
        try:
            async with DistributedLock(f"delivery:{delivery_id}", timeout=10):
                async with get_serializable_transaction() as conn:
                    delivery = await conn.fetchrow(
                        "SELECT id, status, order_id FROM deliveries WHERE id = $1 FOR UPDATE",
                        delivery_id,
                    )
                    if not delivery:
                        raise NotFoundError("Delivery", delivery_id)

                    validate_delivery_transition(delivery["status"], DeliveryStatus.ASSIGNED.value)

                    # Check partner availability
                    partner = await conn.fetchrow(
                        "SELECT id, name, status FROM delivery_partners WHERE id = $1 AND is_active = true FOR UPDATE",
                        partner_id,
                    )
                    if not partner:
                        raise NotFoundError("Delivery partner", partner_id)
                    if partner["status"] == "busy":
                        raise ValidationError(f"Partner {partner['name']} is currently busy")

                    now = datetime.now(timezone.utc)
                    await conn.execute(
                        """
                        UPDATE deliveries
                        SET partner_id = $1, status = 'assigned'::delivery_status, assigned_at = $2
                        WHERE id = $3
                        """,
                        partner_id, now, delivery_id,
                    )

                    await conn.execute(
                        "UPDATE delivery_partners SET status = 'busy'::partner_status WHERE id = $1",
                        partner_id,
                    )

                await emit_and_publish(DomainEvent(
                    event_type=DELIVERY_ASSIGNED,
                    payload={
                        "delivery_id": delivery_id,
                        "partner_id": partner_id,
                        "partner_name": partner["name"],
                        "order_id": str(delivery["order_id"]),
                    },
                    user_id=user.user_id,
                    restaurant_id=user.restaurant_id,
                ))

                return {"delivery_id": delivery_id, "status": "assigned", "partner": partner["name"]}

        except LockError:
            raise LockAcquisitionError(f"delivery:{delivery_id}")

    async def update_status(
        self,
        user: UserContext,
        delivery_id: str,
        new_status: str,
    ) -> dict:
        """Update delivery status with state machine validation."""
        try:
            async with DistributedLock(f"delivery:{delivery_id}", timeout=10):
                async with get_serializable_transaction() as conn:
                    delivery = await conn.fetchrow(
                        "SELECT id, status, partner_id, order_id FROM deliveries WHERE id = $1 FOR UPDATE",
                        delivery_id,
                    )
                    if not delivery:
                        raise NotFoundError("Delivery", delivery_id)

                    target = validate_delivery_transition(delivery["status"], new_status)
                    now = datetime.now(timezone.utc)

                    # Set timestamps based on status
                    extra_fields = ""
                    if target == DeliveryStatus.PICKED_UP:
                        extra_fields = ", picked_up_at = $3"
                    elif target == DeliveryStatus.DELIVERED:
                        extra_fields = ", delivered_at = $3"

                    await conn.execute(
                        f"""
                        UPDATE deliveries SET status = $1::delivery_status {extra_fields}
                        WHERE id = $2
                        """,
                        target.value, delivery_id, *([now] if extra_fields else []),
                    )

                    # Free partner on delivery/failure
                    if target in (DeliveryStatus.DELIVERED, DeliveryStatus.FAILED):
                        if delivery["partner_id"]:
                            await conn.execute(
                                "UPDATE delivery_partners SET status = 'available'::partner_status WHERE id = $1",
                                str(delivery["partner_id"]),
                            )

                    # Update order status for delivery
                    if target == DeliveryStatus.OUT_FOR_DELIVERY:
                        await conn.execute(
                            "UPDATE orders SET status = 'Out for Delivery', updated_at = $1 WHERE id = $2",
                            now, str(delivery["order_id"]),
                        )
                    elif target == DeliveryStatus.DELIVERED:
                        await conn.execute(
                            "UPDATE orders SET status = 'Delivered', updated_at = $1 WHERE id = $2",
                            now, str(delivery["order_id"]),
                        )

                await emit_and_publish(DomainEvent(
                    event_type=DELIVERY_STATUS_CHANGED,
                    payload={
                        "delivery_id": delivery_id,
                        "from_status": delivery["status"],
                        "to_status": target.value,
                        "order_id": str(delivery["order_id"]),
                    },
                    user_id=user.user_id,
                    restaurant_id=user.restaurant_id,
                ))

                return {"delivery_id": delivery_id, "status": target.value}

        except LockError:
            raise LockAcquisitionError(f"delivery:{delivery_id}")

    async def update_location(
        self,
        partner_id: str,
        latitude: float,
        longitude: float,
    ) -> dict:
        """
        Update delivery partner's GPS location.
        Stored in DB + published to Redis for real-time tracking.
        High frequency — optimized for throughput.
        """
        async with get_connection() as conn:
            # Update partner location
            await conn.execute(
                "UPDATE delivery_partners SET latitude = $1, longitude = $2 WHERE id = $3",
                latitude, longitude, partner_id,
            )

            # Find active delivery
            delivery = await conn.fetchrow(
                """
                SELECT id, order_id FROM deliveries
                WHERE partner_id = $1 AND status IN ('assigned', 'picked_up', 'out_for_delivery')
                LIMIT 1
                """,
                partner_id,
            )

            if delivery:
                await conn.execute(
                    """
                    INSERT INTO delivery_tracking (delivery_id, partner_id, latitude, longitude)
                    VALUES ($1, $2, $3, $4)
                    """,
                    str(delivery["id"]), partner_id, latitude, longitude,
                )

        # Publish to Redis for real-time WebSocket tracking
        if delivery:
            await emit_and_publish(DomainEvent(
                event_type=DELIVERY_LOCATION_UPDATED,
                payload={
                    "delivery_id": str(delivery["id"]),
                    "order_id": str(delivery["order_id"]),
                    "partner_id": partner_id,
                    "latitude": latitude,
                    "longitude": longitude,
                },
            ))

        return {"status": "updated"}

    async def get_active_deliveries(self, user: UserContext) -> list[dict]:
        clause, params = tenant_where_clause(user, "d")
        # For deliveries, filter by restaurant_id instead
        # since deliveries use restaurant_id not user_id
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT d.*, dp.name as partner_name, dp.phone as partner_phone,
                       o.order_number, o.total_amount
                FROM deliveries d
                LEFT JOIN delivery_partners dp ON dp.id = d.partner_id
                JOIN orders o ON o.id = d.order_id
                WHERE d.restaurant_id = $1
                  AND d.status NOT IN ('delivered', 'failed')
                ORDER BY d.created_at DESC
                """,
                user.restaurant_id,
            )
            return [dict(r) for r in rows]

    # ── Delivery Partners CRUD ──────────────────────────────────

    async def list_partners(self, user: UserContext) -> list[dict]:
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT id, name, phone, status, is_active, latitude, longitude, created_at
                FROM delivery_partners
                WHERE restaurant_id = $1
                ORDER BY name
                """,
                user.restaurant_id,
            )
            return [dict(r) for r in rows]

    async def create_partner(
        self,
        user: UserContext,
        name: str,
        phone: str,
    ) -> dict:
        import uuid

        partner_id = str(uuid.uuid4())
        async with get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO delivery_partners (id, restaurant_id, name, phone, status, is_active)
                VALUES ($1, $2, $3, $4, 'available'::partner_status, true)
                """,
                partner_id, user.restaurant_id, name, phone,
            )
        return {"id": partner_id, "name": name, "phone": phone, "status": "available", "is_active": True}

    async def update_partner(
        self,
        user: UserContext,
        partner_id: str,
        name: Optional[str] = None,
        phone: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> dict:
        async with get_connection() as conn:
            partner = await conn.fetchrow(
                "SELECT * FROM delivery_partners WHERE id = $1 AND restaurant_id = $2",
                partner_id, user.restaurant_id,
            )
            if not partner:
                raise NotFoundError("Delivery partner", partner_id)

            sets, vals, idx = [], [], 1
            if name is not None:
                sets.append(f"name = ${idx}")
                vals.append(name)
                idx += 1
            if phone is not None:
                sets.append(f"phone = ${idx}")
                vals.append(phone)
                idx += 1
            if is_active is not None:
                sets.append(f"is_active = ${idx}")
                vals.append(is_active)
                idx += 1

            if sets:
                vals.append(partner_id)
                await conn.execute(
                    f"UPDATE delivery_partners SET {', '.join(sets)} WHERE id = ${idx}",
                    *vals,
                )

            updated = await conn.fetchrow(
                "SELECT id, name, phone, status, is_active FROM delivery_partners WHERE id = $1",
                partner_id,
            )
            return dict(updated)

    async def delete_partner(self, user: UserContext, partner_id: str) -> dict:
        async with get_connection() as conn:
            partner = await conn.fetchrow(
                "SELECT id FROM delivery_partners WHERE id = $1 AND restaurant_id = $2",
                partner_id, user.restaurant_id,
            )
            if not partner:
                raise NotFoundError("Delivery partner", partner_id)

            await conn.execute("DELETE FROM delivery_partners WHERE id = $1", partner_id)
        return {"deleted": True}
