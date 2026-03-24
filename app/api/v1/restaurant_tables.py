"""Restaurant Tables endpoints."""
from fastapi import APIRouter, Depends, Query

from app.core.auth import UserContext, require_role
from app.core.database import get_connection
from app.core.logging import get_logger

router = APIRouter(prefix="/restaurant-tables", tags=["Restaurant Tables"])
logger = get_logger(__name__)


@router.get("")
async def list_tables(
    include_orders: bool = Query(False),
    user: UserContext = Depends(require_role("owner", "manager", "cashier", "chef", "waiter", "staff")),
):
    """List all tables for the current user's restaurant."""
    try:
        owner_id = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT rt.*
                FROM restaurant_tables rt
                WHERE rt.user_id = $1
                ORDER BY rt.table_number ASC
                """,
                owner_id,
            )
            tables = [dict(r) for r in rows]

            if include_orders and tables:
                try:
                    table_ids = [t["id"] for t in tables]
                    sessions = await conn.fetch(
                        """
                        SELECT ts.table_id, ts.id as session_id, ts.status as session_status,
                               ts.guest_count, ts.started_at
                        FROM table_sessions ts
                        WHERE ts.table_id = ANY($1) AND ts.is_active = true
                        """,
                        table_ids,
                    )
                    session_map = {str(s["table_id"]): dict(s) for s in sessions}
                    for t in tables:
                        t["active_session"] = session_map.get(str(t["id"]))
                except Exception:
                    pass  # Sessions table may not exist yet

            return tables
    except Exception as e:
        logger.warning("list_tables_failed", error=str(e), user_id=user.user_id)
        return []
