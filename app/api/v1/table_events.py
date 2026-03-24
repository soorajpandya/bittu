"""Table Events endpoints — activity log for table sessions."""
from typing import Optional
from fastapi import APIRouter, Depends, Query

from app.core.auth import UserContext, require_role
from app.core.database import get_connection
from app.core.logging import get_logger

router = APIRouter(prefix="/table-events", tags=["Table Events"])
logger = get_logger(__name__)


@router.get("")
async def list_table_events(
    limit: int = Query(20, ge=1, le=100),
    order_by: str = Query("created_at"),
    ascending: bool = Query(False),
    user: UserContext = Depends(require_role("owner", "manager", "cashier", "chef", "waiter", "staff")),
):
    """List recent table session events / activity log."""
    try:
        owner_id = user.owner_id if user.is_branch_user else user.user_id
        allowed_cols = {"created_at", "table_number", "status", "started_at", "ended_at"}
        col = order_by if order_by in allowed_cols else "created_at"
        direction = "ASC" if ascending else "DESC"
        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""
                SELECT ts.id, ts.table_id, rt.table_number,
                       ts.status, ts.guest_count, ts.started_at, ts.ended_at, ts.created_at
                FROM table_sessions ts
                JOIN restaurant_tables rt ON rt.id = ts.table_id
                WHERE ts.user_id = $1
                ORDER BY ts.{col} {direction}
                LIMIT $2
                """,
                owner_id, limit,
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("list_table_events_failed", error=str(e), user_id=user.user_id)
        return []
