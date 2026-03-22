"""
Notification & Alert Service.

Handles:
  - In-app alerts (low stock, payment issues, order updates)
  - Real-time push via WebSocket/Supabase Realtime
  - Alert acknowledgement and dismissal
"""
from typing import Optional

from app.core.auth import UserContext
from app.core.database import get_connection
from app.core.events import DomainEvent, emit_and_publish, ALERT_CREATED
from app.core.tenant import tenant_where_clause
from app.core.logging import get_logger

logger = get_logger(__name__)


class NotificationService:

    async def create_alert(
        self,
        user_id: str,
        branch_id: Optional[str],
        alert_type: str,
        severity: str,
        title: str,
        message: Optional[str] = None,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
    ) -> dict:
        """Create an in-app alert."""
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO alerts (user_id, branch_id, type, severity, title, message, reference_type, reference_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id, type, severity, title, created_at
                """,
                user_id, branch_id, alert_type, severity, title, message,
                reference_type, reference_id,
            )

            await emit_and_publish(DomainEvent(
                event_type=ALERT_CREATED,
                payload={"alert_id": row["id"], "type": alert_type, "severity": severity, "title": title},
                user_id=user_id,
            ))

            return dict(row)

    async def get_alerts(
        self,
        user: UserContext,
        unread_only: bool = False,
        limit: int = 50,
    ) -> list[dict]:
        """Get alerts for the current user."""
        params = [user.user_id, limit]
        condition = "user_id = $1 AND is_dismissed = false"
        if unread_only:
            condition += " AND is_read = false"

        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""
                SELECT * FROM alerts
                WHERE {condition}
                ORDER BY created_at DESC
                LIMIT $2
                """,
                *params,
            )
            return [dict(r) for r in rows]

    async def mark_read(self, user: UserContext, alert_id: int) -> dict:
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE alerts SET is_read = true WHERE id = $1 AND user_id = $2",
                alert_id, user.user_id,
            )
            return {"id": alert_id, "is_read": True}

    async def dismiss(self, user: UserContext, alert_id: int) -> dict:
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE alerts SET is_dismissed = true WHERE id = $1 AND user_id = $2",
                alert_id, user.user_id,
            )
            return {"id": alert_id, "is_dismissed": True}

    async def mark_all_read(self, user: UserContext) -> dict:
        async with get_connection() as conn:
            result = await conn.execute(
                "UPDATE alerts SET is_read = true WHERE user_id = $1 AND is_read = false",
                user.user_id,
            )
            return {"updated": result}
