"""Table Sessions endpoints — list active/recent sessions."""
from typing import Optional
from fastapi import APIRouter, Depends, Query

from app.core.auth import UserContext, get_current_user
from app.core.database import get_connection
from app.core.logging import get_logger

router = APIRouter(prefix="/table-sessions", tags=["Table Sessions"])
logger = get_logger(__name__)


@router.get("")
async def list_table_sessions(
    table_ids: Optional[str] = Query(None, description="Comma-separated table UUIDs"),
    is_active: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(get_current_user),
):
    """List table sessions, optionally filtered by table IDs and active status."""
    try:
        owner_id = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            conditions = ["ts.user_id = $1"]
            params: list = [owner_id]

            if table_ids:
                ids = [t.strip() for t in table_ids.split(",") if t.strip()]
                if ids:
                    params.append(ids)
                    conditions.append(f"ts.table_id = ANY(${len(params)})")

            if is_active is not None:
                params.append(is_active)
                conditions.append(f"ts.is_active = ${len(params)}")

            params.append(limit)
            where = " AND ".join(conditions)

            rows = await conn.fetch(
                f"""
                SELECT
                    ts.*,
                    rt.table_number,
                    ds.id AS dinein_session_id
                FROM table_sessions ts
                JOIN restaurant_tables rt ON rt.id = ts.table_id
                LEFT JOIN LATERAL (
                    SELECT id
                    FROM dine_in_sessions
                    WHERE table_id = ts.table_id
                      AND user_id = ts.user_id
                      AND status = 'active'
                    ORDER BY created_at DESC
                    LIMIT 1
                ) ds ON true
                WHERE {where}
                ORDER BY ts.started_at DESC
                LIMIT ${len(params)}
                """,
                *params,
            )
            result: list[dict] = []
            for r in rows:
                d = dict(r)
                # Compatibility: frontend expects a usable `session_id` for bill/payments/vacate.
                # Prefer active dine-in session id when present; fall back to whatever the legacy row has.
                if d.get("dinein_session_id"):
                    d["session_id"] = str(d["dinein_session_id"])
                result.append(d)
            return result
    except Exception as e:
        logger.warning("list_table_sessions_failed", error=str(e), user_id=user.user_id)
        return []
