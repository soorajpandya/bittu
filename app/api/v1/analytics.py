"""Analytics endpoints."""
from datetime import date, timedelta
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, require_role
from app.services.analytics_service import AnalyticsService

router = APIRouter(prefix="/analytics", tags=["Analytics"])
_svc = AnalyticsService()


@router.get("/dashboard")
async def dashboard(
    branch_id: str,
    start_date: date = Query(default_factory=lambda: date.today() - timedelta(days=7)),
    end_date: date = Query(default_factory=date.today),
    user: UserContext = Depends(require_role("manager")),
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
    user: UserContext = Depends(require_role("manager")),
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
    user: UserContext = Depends(require_role("manager")),
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
    user: UserContext = Depends(require_role("owner", "manager", "cashier", "chef", "waiter", "staff")),
):
    """Track a user funnel event (onboarding, feature adoption, etc.)."""
    return await _svc.track_funnel_event(
        user=user,
        event=body.event,
        step=body.step,
        metadata=body.metadata,
    )
