"""
Super-admin scheduler controls.

Prefix:   /super-admin/schedulers
Gating:   require_platform_admin()

Lets the platform team list known auto-poll schedulers, trigger a
single-tick run on demand, and see the audit history of those triggers.
The auto-loop continues to run at its normal interval — this is an
escape hatch for "sync right now".
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.core.auth import UserContext, require_platform_admin
from app.core.logging import get_logger
from app.services.super_admin import scheduler_registry

router = APIRouter(
    prefix="/super-admin/schedulers", tags=["Super Admin · Schedulers"],
)
logger = get_logger(__name__)


@router.get("")
async def list_schedulers(
    _: UserContext = Depends(require_platform_admin()),
):
    return {"items": scheduler_registry.list_schedulers()}


@router.post("/{name}/trigger")
async def trigger_scheduler(
    name: str = Path(..., min_length=1, max_length=64),
    actor: UserContext = Depends(require_platform_admin()),
):
    try:
        return await scheduler_registry.trigger(
            name,
            triggered_by=actor.user_id,
            triggered_by_email=actor.email,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/runs")
async def list_runs(
    scheduler_name: Optional[str] = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=500),
    _: UserContext = Depends(require_platform_admin()),
):
    items = await scheduler_registry.list_recent_runs(
        scheduler_name=scheduler_name, limit=limit,
    )
    return {"items": items, "count": len(items)}
