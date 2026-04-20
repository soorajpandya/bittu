"""
Waitlist API endpoints.

POST   /waitlist                — add customer to waitlist
GET    /waitlist                — get current queue
GET    /waitlist/stats          — today's stats
GET    /waitlist/history        — historical entries
POST   /waitlist/notify-next    — notify best-fit customer
POST   /waitlist/{id}/seat      — seat a customer
POST   /waitlist/{id}/skip      — skip a customer
PATCH  /waitlist/{id}/cancel    — cancel an entry
PUT    /waitlist/reorder        — admin reorder queue
GET    /waitlist/settings       — get waitlist settings
PUT    /waitlist/settings       — update waitlist settings
GET    /waitlist/display/{rid}  — public display screen data
GET    /waitlist/status/{id}    — public entry status (QR customer)
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.services.waitlist_service import WaitlistService

router = APIRouter(prefix="/waitlist", tags=["Waitlist"])
_svc = WaitlistService()


# ── Request / Response models ─────────────────────────────────

class AddEntryRequest(BaseModel):
    customer_name: str = Field(..., min_length=1, max_length=100)
    party_size: int = Field(..., ge=1, le=50)
    phone: Optional[str] = Field(None, max_length=20)
    source: str = Field("staff", pattern=r"^(staff|qr)$")
    notes: Optional[str] = Field(None, max_length=500)


class ReorderRequest(BaseModel):
    ordered_ids: list[UUID] = Field(..., min_length=1)


class SettingsUpdate(BaseModel):
    notify_expiry_minutes: Optional[int] = Field(None, ge=1, le=60)
    avg_turnover_minutes: Optional[int] = Field(None, ge=5, le=180)
    sms_enabled: Optional[bool] = None
    whatsapp_enabled: Optional[bool] = None
    display_screen_enabled: Optional[bool] = None
    qr_entry_enabled: Optional[bool] = None
    auto_notify: Optional[bool] = None
    best_fit_enabled: Optional[bool] = None
    display_message: Optional[str] = Field(None, max_length=200)


class NotifyNextRequest(BaseModel):
    table_id: Optional[UUID] = None


# ── Authenticated endpoints ──────────────────────────────────

@router.post("")
async def add_to_waitlist(
    body: AddEntryRequest,
    user: UserContext = Depends(require_permission("waitlist.read")),
):
    """Add a customer to the waitlist."""
    return await _svc.add_entry(
        user,
        customer_name=body.customer_name,
        party_size=body.party_size,
        phone=body.phone,
        source=body.source,
        notes=body.notes,
    )


@router.get("")
async def get_queue(
    status: Optional[str] = Query(None, pattern=r"^(waiting|notified|seated|skipped|cancelled)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("waitlist.read")),
):
    """Get current waitlist queue."""
    return await _svc.get_queue(user, status=status, limit=limit, offset=offset)


@router.get("/stats")
async def get_stats(
    user: UserContext = Depends(require_permission("waitlist.admin")),
):
    """Today's waitlist statistics."""
    return await _svc.get_stats(user)


@router.get("/history")
async def get_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    user: UserContext = Depends(require_permission("waitlist.admin")),
):
    """Get waitlist history."""
    return await _svc.get_history(user, limit=limit, offset=offset,
                                   date_from=date_from, date_to=date_to)


@router.post("/notify-next")
async def notify_next(
    body: NotifyNextRequest = NotifyNextRequest(),
    user: UserContext = Depends(require_permission("waitlist.manage")),
):
    """Find best-fit customer for available table and notify them."""
    result = await _svc.notify_next(user, table_id=body.table_id)
    if not result:
        raise HTTPException(404, "No matching customer or no available table")
    return result


@router.post("/expire-check")
async def expire_check(
    user: UserContext = Depends(require_permission("waitlist.read")),
):
    """Check and expire overdue notified entries."""
    return {"expired": await _svc.expire_overdue(user)}


@router.post("/{entry_id}/seat")
async def seat_customer(
    entry_id: UUID,
    user: UserContext = Depends(require_permission("waitlist.manage")),
):
    """Seat a waitlisted customer."""
    return await _svc.seat_customer(user, entry_id)


@router.post("/{entry_id}/skip")
async def skip_customer(
    entry_id: UUID,
    reason: str = Query("no_show"),
    user: UserContext = Depends(require_permission("waitlist.manage")),
):
    """Skip a waitlisted customer (no-show or manual)."""
    return await _svc.skip_customer(user, entry_id, reason=reason)


@router.patch("/{entry_id}/cancel")
async def cancel_entry(
    entry_id: UUID,
    user: UserContext = Depends(require_permission("waitlist.read")),
):
    """Cancel a waitlist entry."""
    return await _svc.cancel_entry(user, entry_id)


@router.put("/reorder")
async def reorder_queue(
    body: ReorderRequest,
    user: UserContext = Depends(require_permission("waitlist.admin")),
):
    """Admin reorder the waitlist queue."""
    return await _svc.reorder(user, body.ordered_ids)


@router.get("/settings")
async def get_settings(
    user: UserContext = Depends(require_permission("waitlist.admin")),
):
    """Get waitlist settings."""
    return await _svc.get_settings(user)


@router.put("/settings")
async def update_settings(
    body: SettingsUpdate,
    user: UserContext = Depends(require_permission("waitlist.admin")),
):
    """Update waitlist settings."""
    return await _svc.update_settings(user, body.model_dump(exclude_none=True))


# ── Public endpoints (no auth) ───────────────────────────────

@router.get("/display/{restaurant_id}")
async def display_screen(restaurant_id: UUID):
    """Public display screen data — shows 'now serving' and queue."""
    return await _svc.get_display_data(restaurant_id)


@router.get("/status/{entry_id}")
async def entry_status(entry_id: UUID):
    """Public entry status — for QR customer to check their position."""
    result = await _svc.get_entry_status(entry_id)
    if not result:
        raise HTTPException(404, "Entry not found")
    return result
