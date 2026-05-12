"""
Audit Events — Merchant API (Phase 6).

Prefix:   /audit/events
Audience: a merchant viewing their own audit trail.

This is the *new* hash-chained audit log (table: ``audit_events``). The
legacy ``/audit-logs`` router reads a different (free-form) table and is
left untouched.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response

from app.core.auth import UserContext, require_permission
from app.core.exceptions import ValidationError
from app.services.audit_service import audit_service

router = APIRouter(prefix="/audit/events", tags=["Audit Events"])


def _merchant_id(user: UserContext) -> str:
    if not user.restaurant_id:
        raise ValidationError("Active restaurant context is required.")
    return str(user.restaurant_id)


@router.get("/")
async def list_audit_events(
    action:        Optional[str] = Query(None),
    actor_user_id: Optional[str] = Query(None),
    resource_type: Optional[str] = Query(None),
    resource_id:   Optional[str] = Query(None),
    from_ts:       Optional[datetime] = Query(None),
    to_ts:         Optional[datetime] = Query(None),
    limit:         int = Query(100, ge=1, le=500),
    offset:        int = Query(0,   ge=0),
    user: UserContext = Depends(require_permission("audit.read")),
):
    return await audit_service.list_events(
        merchant_id=_merchant_id(user),
        action=action,
        actor_user_id=actor_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        from_ts=from_ts,
        to_ts=to_ts,
        limit=limit,
        offset=offset,
    )


@router.get("/{event_uuid}")
async def get_audit_event(
    event_uuid: str,
    user: UserContext = Depends(require_permission("audit.read")),
):
    return await audit_service.get_event(
        event_uuid=event_uuid,
        merchant_id=_merchant_id(user),
    )


@router.get("/csv")
async def list_audit_events_csv(
    action:        Optional[str] = Query(None),
    from_ts:       Optional[datetime] = Query(None),
    to_ts:         Optional[datetime] = Query(None),
    limit:         int = Query(500, ge=1, le=500),
    user: UserContext = Depends(require_permission("audit.read")),
):
    events = await audit_service.list_events(
        merchant_id=_merchant_id(user),
        action=action, from_ts=from_ts, to_ts=to_ts,
        limit=limit, offset=0,
    )
    out = audit_service.to_csv(events)
    return Response(
        content=out["body"],
        media_type=out["content_type"],
        headers={
            "Content-Disposition": f'attachment; filename="{out["filename"]}"',
        },
    )
