"""Table Events endpoints — activity log for table sessions."""
from typing import Optional
from fastapi import APIRouter, Depends, Query

from app.core.auth import UserContext, get_current_user
from app.core.database import get_connection
from app.core.logging import get_logger

router = APIRouter(prefix="/table-events", tags=["Table Events"])
logger = get_logger(__name__)


@router.get("")
async def list_table_events(
    table_id: Optional[str] = None,
    session_id: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
    order_by: str = Query("created_at"),
    ascending: bool = Query(False),
    user: UserContext = Depends(get_current_user),
):
    """List table events from the table_events table."""
    try:
        owner_id = user.owner_id if user.is_branch_user else user.user_id
        direction = "ASC" if ascending else "DESC"

        conditions = ["te.user_id = $1"]
        params: list = [owner_id]
        idx = 2

        if table_id:
            conditions.append(f"te.table_id = ${idx}")
            params.append(table_id)
            idx += 1

        if session_id:
            conditions.append(f"te.session_id = ${idx}")
            params.append(session_id)
            idx += 1

        if event_type:
            conditions.append(f"te.event_type = ${idx}")
            params.append(event_type)
            idx += 1

        where = " AND ".join(conditions)
        params.append(limit)

        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""
                SELECT te.id, te.table_id, te.session_id,
                       te.event_type, te.payload, te.created_at
                FROM table_events te
                WHERE {where}
                ORDER BY te.created_at {direction}
                LIMIT ${idx}
                """,
                *params,
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("list_table_events_failed", error=str(e), user_id=user.user_id)
        return []
