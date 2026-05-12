"""
Disputes — Admin API (Phase 7).

Prefix:   /admin/disputes
Audience: platform admins. Cross-merchant access; merchant_id is an OPTIONAL
filter, not a constraint.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from app.core.auth import require_platform_admin
from app.services.dispute_service import dispute_service

router = APIRouter(prefix="/admin/disputes", tags=["Admin Disputes"])


class _AdminOpenBody(BaseModel):
    merchant_id: str
    kind: str
    amount: Decimal = Field(..., gt=0)
    payment_id: Optional[str] = None
    order_id: Optional[str] = None
    refund_id: Optional[int] = None
    currency: str = "INR"
    customer_reference: Optional[str] = None
    bank_case_id: Optional[str] = None
    evidence: Optional[dict] = None
    notes: Optional[dict] = None
    due_at: Optional[datetime] = None
    assigned_admin_id: Optional[str] = None


class _AdminTransitionBody(BaseModel):
    new_status: str
    outcome: Optional[str] = None
    resolution_notes: Optional[str] = None
    evidence_patch: Optional[dict] = None
    notes_patch: Optional[dict] = None


class _AdminAssignBody(BaseModel):
    assigned_admin_id: str


class _AdminNoteBody(BaseModel):
    note: str


@router.post("/")
async def admin_open_dispute(
    body: _AdminOpenBody,
    admin = Depends(require_platform_admin()),
):
    return await dispute_service.open_dispute(
        merchant_id=body.merchant_id,
        kind=body.kind,
        amount=body.amount,
        payment_id=body.payment_id,
        order_id=body.order_id,
        refund_id=body.refund_id,
        currency=body.currency,
        customer_reference=body.customer_reference,
        bank_case_id=body.bank_case_id,
        evidence=body.evidence,
        notes=body.notes,
        due_at=body.due_at,
        opened_by_admin_id=getattr(admin, "user_id", None),
        assigned_admin_id=body.assigned_admin_id,
    )


@router.get("/")
async def admin_list_disputes(
    merchant_id: Optional[str] = Query(None),
    status:      Optional[str] = Query(None),
    kind:        Optional[str] = Query(None),
    payment_id:  Optional[str] = Query(None),
    assigned_admin_id: Optional[str] = Query(None),
    from_ts:     Optional[datetime] = Query(None),
    to_ts:       Optional[datetime] = Query(None),
    limit:       int = Query(100, ge=1, le=500),
    offset:      int = Query(0, ge=0),
    _ = Depends(require_platform_admin()),
):
    return await dispute_service.list_disputes(
        merchant_id=merchant_id, status=status, kind=kind,
        payment_id=payment_id, assigned_admin_id=assigned_admin_id,
        from_ts=from_ts, to_ts=to_ts, limit=limit, offset=offset,
    )


@router.get("/csv")
async def admin_export_disputes_csv(
    merchant_id: Optional[str] = Query(None),
    status:      Optional[str] = Query(None),
    kind:        Optional[str] = Query(None),
    from_ts:     Optional[datetime] = Query(None),
    to_ts:       Optional[datetime] = Query(None),
    limit:       int = Query(500, ge=1, le=500),
    _ = Depends(require_platform_admin()),
):
    rows = await dispute_service.list_disputes(
        merchant_id=merchant_id, status=status, kind=kind,
        from_ts=from_ts, to_ts=to_ts, limit=limit,
    )
    out = dispute_service.to_csv(rows)
    return Response(
        content=out["body"],
        media_type=out["content_type"],
        headers={"Content-Disposition": f'attachment; filename="{out["filename"]}"'},
    )


@router.get("/{dispute_id}")
async def admin_get_dispute(
    dispute_id: int,
    _ = Depends(require_platform_admin()),
):
    return await dispute_service.get(dispute_id)


@router.get("/{dispute_id}/events")
async def admin_list_dispute_events(
    dispute_id: int,
    _ = Depends(require_platform_admin()),
):
    return await dispute_service.list_events(dispute_id)


@router.post("/{dispute_id}/transition")
async def admin_transition_dispute(
    dispute_id: int,
    body: _AdminTransitionBody,
    admin = Depends(require_platform_admin()),
):
    return await dispute_service.transition(
        dispute_id,
        merchant_id=None,
        new_status=body.new_status,
        outcome=body.outcome,
        resolution_notes=body.resolution_notes,
        evidence_patch=body.evidence_patch,
        notes_patch=body.notes_patch,
        actor_admin_id=getattr(admin, "user_id", None),
    )


@router.post("/{dispute_id}/assign")
async def admin_assign_dispute(
    dispute_id: int,
    body: _AdminAssignBody,
    admin = Depends(require_platform_admin()),
):
    return await dispute_service.assign(
        dispute_id, merchant_id=None,
        assigned_admin_id=body.assigned_admin_id,
        actor_admin_id=getattr(admin, "user_id", None),
    )


@router.post("/{dispute_id}/notes")
async def admin_add_dispute_note(
    dispute_id: int,
    body: _AdminNoteBody,
    admin = Depends(require_platform_admin()),
):
    return await dispute_service.add_note(
        dispute_id, merchant_id=None, note=body.note,
        actor_admin_id=getattr(admin, "user_id", None),
    )
