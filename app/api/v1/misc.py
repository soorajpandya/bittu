"""Sync Logs, Payment Reminders, Trial, Funnel, Session Devices endpoints (read-only)."""
from typing import Optional
from fastapi import APIRouter, Depends, Query

from app.core.auth import UserContext, require_permission
from app.services.misc_service import (
    SyncLogService,
    PaymentReminderService,
    TrialEligibilityService,
    UserFunnelService,
    TableSessionDeviceService,
)

router = APIRouter(prefix="/misc", tags=["Miscellaneous"])
_sync = SyncLogService()
_reminders = PaymentReminderService()
_trial = TrialEligibilityService()
_funnel = UserFunnelService()
_sessions = TableSessionDeviceService()


@router.get("/sync-logs")
async def list_sync_logs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("settings.read")),
):
    return await _sync.list_sync_logs(user, limit=limit, offset=offset)


@router.get("/payment-reminders")
async def list_payment_reminders(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("settings.admin")),
):
    return await _reminders.list_reminders(user, limit=limit, offset=offset)


@router.get("/trial-status")
async def get_trial_status(
    user: UserContext = Depends(require_permission("settings.admin")),
):
    return await _trial.get_trial_status(user)


@router.get("/funnel-events")
async def list_funnel_events(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("settings.admin")),
):
    return await _funnel.list_events(user, limit=limit, offset=offset)


@router.get("/session-devices")
async def list_session_devices(
    session_id: Optional[str] = None,
    user: UserContext = Depends(require_permission("settings.read")),
):
    return await _sessions.list_devices(user, session_id=session_id)
