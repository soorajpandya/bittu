"""
Kitchen Display System (KDS) Service.

Real-time kitchen order management:
  - Orders flow in from order creation triggers
  - Kitchen staff update item/order status
  - Updates broadcast in real-time to all KDS screens
  - Station-based routing (grill, drinks, desserts, etc.)

Performance:
  - KDS screens poll via WebSocket, not HTTP
  - Redis pub/sub for instant updates across devices
  - Optimistic reads from cache, writes go to DB

State machine: queued → preparing → ready → served
"""
from datetime import datetime, timezone
from typing import Optional

from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.redis import DistributedLock, LockError
from app.core.state_machines import KitchenStatus, validate_kitchen_transition
from app.core.events import (
    DomainEvent, emit_and_publish,
    KITCHEN_STATUS_CHANGED, KITCHEN_ITEM_READY,
)
from app.core.tenant import tenant_where_clause
from app.core.exceptions import NotFoundError, LockAcquisitionError
from app.core.logging import get_logger

logger = get_logger(__name__)


class KitchenService:

    async def get_active_orders(
        self,
        user: UserContext,
        station_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict]:
        """
        Get active kitchen orders for a branch.
        Optionally filter by station and status.
        """
        clause, params = tenant_where_clause(user, "ko")

        conditions = [clause, "ko.status != 'served'"]
        if station_id:
            params.append(station_id)
            conditions.append(f"ko.station = ${len(params)}")
        if status:
            params.append(status)
            conditions.append(f"ko.status = ${len(params)}::kitchen_status")

        where = " AND ".join(conditions)

        async with get_connection() as conn:
            orders = await conn.fetch(
                f"""
                SELECT ko.*,
                       o.order_number, o.table_number, o.source,
                       o.notes as order_notes
                FROM kitchen_orders ko
                JOIN orders o ON o.id = ko.order_id
                WHERE {where}
                ORDER BY ko.priority DESC, ko.created_at ASC
                """,
                *params,
            )

            result = []
            for ko in orders:
                ko_dict = dict(ko)
                items = await conn.fetch(
                    """
                    SELECT koi.*, ism.station_id
                    FROM kitchen_order_items koi
                    LEFT JOIN item_station_mapping ism
                        ON ism.item_id = (
                            SELECT item_id FROM order_items WHERE id = koi.order_item_id
                        )
                    WHERE koi.kitchen_order_id = $1
                    ORDER BY koi.id
                    """,
                    str(ko["id"]),
                )
                ko_dict["items"] = [dict(i) for i in items]
                result.append(ko_dict)

            return result

    async def update_order_status(
        self,
        user: UserContext,
        kitchen_order_id: str,
        new_status: str,
    ) -> dict:
        """Update kitchen order status with state machine validation."""
        try:
            async with DistributedLock(f"kds:{kitchen_order_id}", timeout=5):
                async with get_serializable_transaction() as conn:
                    ko = await conn.fetchrow(
                        """
                        SELECT id, order_id, status, user_id
                        FROM kitchen_orders WHERE id = $1
                        FOR UPDATE
                        """,
                        kitchen_order_id,
                    )
                    if not ko:
                        raise NotFoundError("Kitchen order", kitchen_order_id)

                    target = validate_kitchen_transition(ko["status"], new_status)
                    now = datetime.now(timezone.utc)

                    update_fields = {"status": target.value}
                    if target == KitchenStatus.PREPARING:
                        update_fields["started_at"] = now
                    elif target == KitchenStatus.READY:
                        update_fields["ready_at"] = now
                    elif target == KitchenStatus.SERVED:
                        update_fields["served_at"] = now

                    set_clauses = ", ".join(
                        f"{k} = ${i+2}" if k != "status"
                        else f"{k} = ${i+2}::kitchen_status"
                        for i, k in enumerate(update_fields.keys())
                    )
                    values = [kitchen_order_id] + list(update_fields.values())

                    await conn.execute(
                        f"UPDATE kitchen_orders SET {set_clauses} WHERE id = $1",
                        *values,
                    )

                    # Also update all items in this order
                    await conn.execute(
                        "UPDATE kitchen_order_items SET status = $1::kitchen_status WHERE kitchen_order_id = $2",
                        target.value, kitchen_order_id,
                    )

                await emit_and_publish(DomainEvent(
                    event_type=KITCHEN_STATUS_CHANGED,
                    payload={
                        "kitchen_order_id": kitchen_order_id,
                        "order_id": str(ko["order_id"]),
                        "from_status": ko["status"],
                        "to_status": target.value,
                    },
                    user_id=user.user_id,
                    restaurant_id=user.restaurant_id,
                    branch_id=user.branch_id,
                ))

                return {"id": kitchen_order_id, "status": target.value}

        except LockError:
            raise LockAcquisitionError(f"kitchen_order:{kitchen_order_id}")

    async def update_item_status(
        self,
        user: UserContext,
        item_id: str,
        new_status: str,
    ) -> dict:
        """Update individual kitchen item status."""
        async with get_serializable_transaction() as conn:
            item = await conn.fetchrow(
                """
                SELECT koi.id, koi.kitchen_order_id, koi.status, koi.item_name
                FROM kitchen_order_items koi
                WHERE koi.id = $1
                FOR UPDATE
                """,
                item_id,
            )
            if not item:
                raise NotFoundError("Kitchen item", item_id)

            target = validate_kitchen_transition(item["status"], new_status)
            now = datetime.now(timezone.utc)

            started = now if target == KitchenStatus.PREPARING else None
            ready = now if target == KitchenStatus.READY else None

            await conn.execute(
                """
                UPDATE kitchen_order_items
                SET status = $1::kitchen_status,
                    started_at = COALESCE($2, started_at),
                    ready_at = COALESCE($3, ready_at)
                WHERE id = $4
                """,
                target.value, started, ready, item_id,
            )

            # Check if all items in this order are ready
            if target == KitchenStatus.READY:
                remaining = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM kitchen_order_items
                    WHERE kitchen_order_id = $1 AND status != 'ready' AND status != 'served'
                    """,
                    str(item["kitchen_order_id"]),
                )
                if remaining == 0:
                    # Auto-transition the parent kitchen order to ready
                    await conn.execute(
                        "UPDATE kitchen_orders SET status = 'ready'::kitchen_status, ready_at = $1 WHERE id = $2",
                        now, str(item["kitchen_order_id"]),
                    )

        if target == KitchenStatus.READY:
            await emit_and_publish(DomainEvent(
                event_type=KITCHEN_ITEM_READY,
                payload={
                    "item_id": item_id,
                    "item_name": item["item_name"],
                    "kitchen_order_id": str(item["kitchen_order_id"]),
                },
                user_id=user.user_id,
                restaurant_id=user.restaurant_id,
                branch_id=user.branch_id,
            ))

        return {"id": item_id, "status": target.value}

    async def get_station_orders(
        self,
        user: UserContext,
        station_id: str,
    ) -> list[dict]:
        """Get orders for a specific kitchen station."""
        async with get_connection() as conn:
            items = await conn.fetch(
                """
                SELECT koi.*, ko.order_id, o.order_number, o.table_number
                FROM kitchen_order_items koi
                JOIN kitchen_orders ko ON ko.id = koi.kitchen_order_id
                JOIN orders o ON o.id = ko.order_id
                WHERE koi.station_id = $1
                  AND koi.status != 'served'
                  AND ko.user_id = $2
                ORDER BY ko.priority DESC, ko.created_at ASC
                """,
                station_id,
                user.owner_id if user.is_branch_user else user.user_id,
            )
            return [dict(i) for i in items]
