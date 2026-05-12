"""
Audit Events — Admin API (Phase 6).

Prefix:   /admin/audit/events
Audience: platform admins. Cross-merchant view + chain verification.

Carry-over rule: admin and merchant APIs are different. Admin endpoints
NEVER apply the merchant-scope filter unless the caller passes one
explicitly via query string.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response

from app.core.auth import require_platform_admin
from app.services.audit_service import audit_service

router = APIRouter(prefix="/admin/audit/events", tags=["Admin Audit Events"])


@router.get("/")
async def admin_list_audit_events(
    merchant_id:   Optional[str] = Query(None),
    action:        Optional[str] = Query(None),
    actor_user_id: Optional[str] = Query(None),
    resource_type: Optional[str] = Query(None),
    resource_id:   Optional[str] = Query(None),
    from_ts:       Optional[datetime] = Query(None),
    to_ts:         Optional[datetime] = Query(None),
    limit:         int = Query(100, ge=1, le=500),
    offset:        int = Query(0,   ge=0),
    _: object = Depends(require_platform_admin()),
):
    return await audit_service.list_events(
        merchant_id=merchant_id,
        action=action,
        actor_user_id=actor_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        from_ts=from_ts,
        to_ts=to_ts,
        limit=limit,
        offset=offset,
    )


@router.get("/verify")
async def admin_verify_chain(
    start_id: Optional[int] = Query(None, ge=1),
    end_id:   Optional[int] = Query(None, ge=1),
    _: object = Depends(require_platform_admin()),
):
    return await audit_service.verify_chain(start_id=start_id, end_id=end_id)


@router.get("/csv")
async def admin_list_audit_events_csv(
    merchant_id:   Optional[str] = Query(None),
    action:        Optional[str] = Query(None),
    from_ts:       Optional[datetime] = Query(None),
    to_ts:         Optional[datetime] = Query(None),
    limit:         int = Query(500, ge=1, le=500),
    _: object = Depends(require_platform_admin()),
):
    events = await audit_service.list_events(
        merchant_id=merchant_id,
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


@router.get("/{event_uuid}")
async def admin_get_audit_event(
    event_uuid: str,
    _: object = Depends(require_platform_admin()),
):
    return await audit_service.get_event(event_uuid=event_uuid)
