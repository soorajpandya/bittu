"""Notification & Alert endpoints."""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user
from app.services.notification_service import NotificationService

router = APIRouter(prefix="/notifications", tags=["Notifications"])
_svc = NotificationService()


@router.get("")
async def list_notifications(
    limit: int = Query(20, ge=1, le=200),
    user: UserContext = Depends(get_current_user),
):
    """List recent notifications/alerts for the current user."""
    return await _svc.get_alerts(user=user, unread_only=False, limit=limit)


@router.get("/alerts")
async def list_alerts(
    unread_only: bool = False,
    limit: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(get_current_user),
):
    return await _svc.get_alerts(user=user, unread_only=unread_only, limit=limit)


@router.patch("/alerts/{alert_id}/read")
async def mark_alert_read(
    alert_id: int,
    user: UserContext = Depends(get_current_user),
):
    return await _svc.mark_read(user=user, alert_id=alert_id)


@router.patch("/alerts/read-all")
async def mark_all_read(
    user: UserContext = Depends(get_current_user),
):
    return await _svc.mark_all_read(user=user)


@router.delete("/alerts/{alert_id}")
async def dismiss_alert(
    alert_id: int,
    user: UserContext = Depends(get_current_user),
):
    return await _svc.dismiss(user=user, alert_id=alert_id)
