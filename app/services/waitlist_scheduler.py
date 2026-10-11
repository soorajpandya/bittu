"""Expire notifications and allocate vacant tables; events wake the periodic worker."""

import asyncio

from app.core.auth import UserContext
from app.core.database import get_service_connection, use_tenant
from app.core.events import TABLE_SESSION_ENDED, TABLE_STATUS_CHANGED, subscribe
from app.core.logging import get_logger
from app.services.waitlist_service import WaitlistService

logger = get_logger(__name__)
_wake = None
_registered = False


async def _on_table_change(event):
    # Do not acquire database locks inside an event publisher's transaction.
    if _wake is not None:
        _wake.set()


async def run_waitlist_tick():
    service = WaitlistService()
    async with get_service_connection() as conn:
        scopes = await conn.fetch(
            "SELECT DISTINCT user_id,restaurant_id,branch_id FROM waitlist_entries "
            "WHERE status IN ('waiting','notified')"
        )
    for scope in scopes:
        user = UserContext(
            user_id=str(scope["user_id"]),
            restaurant_id=str(scope["restaurant_id"]),
            branch_id=str(scope["branch_id"]) if scope["branch_id"] else None,
        )
        try:
            async with use_tenant(user.user_id):
                await service.expire_overdue(user)
                await service.refresh_estimates(user)
                # A bounded batch prevents one large queue starving other sites.
                for _ in range(100):
                    if not await service.on_table_freed(user, None):
                        break
        except Exception:
            logger.exception(
                "waitlist_worker_scope_failed", restaurant_id=user.restaurant_id, branch_id=user.branch_id
            )

    from app.services.push_service import run_delivery_tick

    await run_delivery_tick()


async def _run():
    while True:
        _wake.clear()
        try:
            await run_waitlist_tick()
        except Exception:
            logger.exception("waitlist_worker_failed")
        try:
            await asyncio.wait_for(_wake.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


def start_waitlist_scheduler():
    global _wake, _registered
    _wake = asyncio.Event()
    if not _registered:
        subscribe(TABLE_SESSION_ENDED, _on_table_change)
        subscribe(TABLE_STATUS_CHANGED, _on_table_change)
        subscribe("waitlist.created", _on_table_change)
        subscribe("waitlist.updated", _on_table_change)
        subscribe("waitlist.settings_updated", _on_table_change)
        _registered = True
    return asyncio.create_task(_run(), name="waitlist-maintenance")
