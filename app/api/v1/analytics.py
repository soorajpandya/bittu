"""Analytics endpoints."""
from datetime import date, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.core.database import get_connection
from app.core.logging import get_logger
from app.services.analytics_service import AnalyticsService

router = APIRouter(prefix="/analytics", tags=["Analytics"])
_svc = AnalyticsService()
logger = get_logger(__name__)

_EMPTY_COUNTS = {
    "total_orders": 0, "completed_orders": 0, "cancelled_orders": 0,
    "total_revenue": 0, "pending_orders": 0,
}


async def _resolve_branch(conn, owner_id: str, branch_id: Optional[str], user_branch_id: Optional[str]) -> Optional[str]:
    """Resolve effective branch_id from explicit param, user context, or main branch lookup."""
    effective = branch_id or user_branch_id
    if effective:
        return effective
    try:
        row = await conn.fetchrow(
            """
            SELECT sb.id FROM sub_branches sb
            JOIN restaurants r ON r.id = sb.restaurant_id
            WHERE r.owner_id = $1 AND sb.is_main_branch = true
            LIMIT 1
            """,
            owner_id,
        )
        return str(row["id"]) if row else None
    except Exception:
        return None


@router.get("/dashboard-counts")
async def dashboard_counts(
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """Return quick dashboard counts (orders, revenue, etc.) for today."""
    try:
        owner_id = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            effective_branch = await _resolve_branch(conn, owner_id, branch_id, user.branch_id)
            if not effective_branch:
                return _EMPTY_COUNTS

            today = date.today()
            counts = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)                                      AS total_orders,
                    COUNT(*) FILTER (WHERE status = 'completed')  AS completed_orders,
                    COUNT(*) FILTER (WHERE status = 'cancelled')  AS cancelled_orders,
                    COALESCE(SUM(total) FILTER (WHERE status = 'completed'), 0) AS total_revenue,
                    COUNT(*) FILTER (WHERE status IN ('pending', 'confirmed', 'preparing')) AS pending_orders
                FROM orders
                WHERE branch_id = $1 AND DATE(created_at) = $2
                """,
                effective_branch, today,
            )
            return dict(counts) if counts else _EMPTY_COUNTS
    except Exception as e:
        logger.warning("dashboard_counts_failed", error=str(e), user_id=user.user_id)
        return _EMPTY_COUNTS


@router.get("/daily")
async def daily_analytics(
    target_date: date = Query(default=None, alias="date"),
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """Return daily analytics for a specific date."""
    if target_date is None:
        target_date = date.today()
    try:
        owner_id = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            effective_branch = await _resolve_branch(conn, owner_id, branch_id, user.branch_id)
            if not effective_branch:
                return {"date": target_date.isoformat(), "total_orders": 0, "total_revenue": 0}

            try:
                row = await conn.fetchrow(
                    "SELECT * FROM daily_analytics WHERE branch_id = $1 AND date = $2",
                    effective_branch, target_date,
                )
                if row:
                    from app.services.analytics_service import _row_to_serializable
                    return _row_to_serializable(row)
            except Exception:
                pass  # Table may not exist yet

            # Fallback: compute from orders table
            counts = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)                                      AS total_orders,
                    COUNT(*) FILTER (WHERE status = 'completed')  AS completed_orders,
                    COUNT(*) FILTER (WHERE status = 'cancelled')  AS cancelled_orders,
                    COALESCE(SUM(total) FILTER (WHERE status = 'completed'), 0) AS total_revenue,
                    COALESCE(SUM(tax)   FILTER (WHERE status = 'completed'), 0) AS total_tax,
                    COALESCE(SUM(discount) FILTER (WHERE status = 'completed'), 0) AS total_discount
                FROM orders
                WHERE branch_id = $1 AND DATE(created_at) = $2
                """,
                effective_branch, target_date,
            )
            result = dict(counts) if counts else {}
            result["date"] = target_date.isoformat()
            result["branch_id"] = effective_branch
            return result
    except Exception as e:
        logger.warning("daily_analytics_failed", error=str(e), user_id=user.user_id)
        return {"date": target_date.isoformat(), "total_orders": 0, "total_revenue": 0}


@router.get("/dashboard")
async def dashboard(
    branch_id: str,
    start_date: date = Query(default_factory=lambda: date.today() - timedelta(days=7)),
    end_date: date = Query(default_factory=date.today),
    user: UserContext = Depends(require_permission("analytics.read")),
):
    return await _svc.get_dashboard(
        user=user,
        branch_id=branch_id,
        start_date=start_date,
        end_date=end_date,
    )


@router.get("/compare")
async def compare_periods(
    branch_id: str,
    current_start: date = Query(...),
    current_end: date = Query(...),
    previous_start: date = Query(...),
    previous_end: date = Query(...),
    user: UserContext = Depends(require_permission("analytics.read")),
):
    return await _svc.compare_periods(
        branch_id=branch_id,
        current_start=current_start,
        current_end=current_end,
        previous_start=previous_start,
        previous_end=previous_end,
    )


@router.get("/heatmap")
async def hourly_heatmap(
    branch_id: str,
    target_date: date = Query(default_factory=date.today),
    user: UserContext = Depends(require_permission("analytics.read")),
):
    return await _svc.get_hourly_heatmap(branch_id=branch_id, target_date=target_date)


class FunnelEventIn(BaseModel):
    event: str = ""
    step: str | None = None
    metadata: dict | None = None
    screen: str | None = None
    action: str | None = None


@router.post("/funnel")
async def track_funnel(
    body: FunnelEventIn = FunnelEventIn(),
    user: UserContext = Depends(get_current_user),
):
    """Track a user funnel event (onboarding, feature adoption, etc.)."""
    return await _svc.track_funnel_event(
        user=user,
        event=body.event,
        step=body.step,
        metadata=body.metadata,
    )
