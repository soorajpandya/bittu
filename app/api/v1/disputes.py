"""Disputes — Merchant API (Phase 7). Prefix: /disputes. Scoped to caller's merchant."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.exceptions import ValidationError
from app.services.dispute_service import dispute_service

router = APIRouter(prefix="/disputes", tags=["Disputes"])


def _mid(user: UserContext) -> str:
    if not user.restaurant_id:
        raise ValidationError("Active restaurant context is required.")
    return str(user.restaurant_id)


class _OpenBody(BaseModel):
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


class _TransitionBody(BaseModel):
    new_status: str
    outcome: Optional[str] = None
    resolution_notes: Optional[str] = None
    evidence_patch: Optional[dict] = None
    notes_patch: Optional[dict] = None


class _NoteBody(BaseModel):
    note: str


@router.post("/")
async def open_dispute(
    body: _OpenBody,
    user: UserContext = Depends(require_permission("disputes.write")),
):
    return await dispute_service.open_dispute(
        merchant_id=_mid(user),
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
        opened_by_user_id=user.user_id,
    )


@router.get("/")
async def list_disputes(
    status:     Optional[str] = Query(None),
    kind:       Optional[str] = Query(None),
    payment_id: Optional[str] = Query(None),
    from_ts:    Optional[datetime] = Query(None),
    to_ts:      Optional[datetime] = Query(None),
    limit:      int = Query(100, ge=1, le=500),
    offset:     int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("disputes.read")),
):
    return await dispute_service.list_disputes(
        merchant_id=_mid(user),
        status=status, kind=kind, payment_id=payment_id,
        from_ts=from_ts, to_ts=to_ts, limit=limit, offset=offset,
    )


@router.get("/csv")
async def export_disputes_csv(
    status:  Optional[str] = Query(None),
    kind:    Optional[str] = Query(None),
    from_ts: Optional[datetime] = Query(None),
    to_ts:   Optional[datetime] = Query(None),
    limit:   int = Query(500, ge=1, le=500),
    user: UserContext = Depends(require_permission("disputes.read")),
):
    rows = await dispute_service.list_disputes(
        merchant_id=_mid(user), status=status, kind=kind,
        from_ts=from_ts, to_ts=to_ts, limit=limit,
    )
    out = dispute_service.to_csv(rows)
    return Response(
        content=out["body"],
        media_type=out["content_type"],
        headers={"Content-Disposition": f'attachment; filename="{out["filename"]}"'},
    )


@router.get("/{dispute_id}")
async def get_dispute(
    dispute_id: int,
    user: UserContext = Depends(require_permission("disputes.read")),
):
    return await dispute_service.get(dispute_id, merchant_id=_mid(user))


@router.get("/{dispute_id}/events")
async def list_dispute_events(
    dispute_id: int,
    user: UserContext = Depends(require_permission("disputes.read")),
):
    return await dispute_service.list_events(dispute_id, merchant_id=_mid(user))


@router.post("/{dispute_id}/transition")
async def transition_dispute(
    dispute_id: int,
    body: _TransitionBody,
    user: UserContext = Depends(require_permission("disputes.write")),
):
    return await dispute_service.transition(
        dispute_id,
        merchant_id=_mid(user),
        new_status=body.new_status,
        outcome=body.outcome,
        resolution_notes=body.resolution_notes,
        evidence_patch=body.evidence_patch,
        notes_patch=body.notes_patch,
        actor_user_id=user.user_id,
    )


@router.post("/{dispute_id}/notes")
async def add_dispute_note(
    dispute_id: int,
    body: _NoteBody,
    user: UserContext = Depends(require_permission("disputes.write")),
):
    return await dispute_service.add_note(
        dispute_id, merchant_id=_mid(user), note=body.note,
        actor_user_id=user.user_id,
    )


# ── Razorpay deep wiring (Phase 5) ────────────────────────────────────────


class _ContestBody(BaseModel):
    evidence: dict
    action: str = "draft"  # draft|submit


@router.post("/{dispute_id}/accept")
async def accept_dispute(
    dispute_id: int,
    user: UserContext = Depends(require_permission("razorpay.disputes.write")),
):
    """Admit liability on a Razorpay dispute (transitions local row to 'lost')."""
    return await dispute_service.accept_via_gateway(
        dispute_id,
        merchant_id=_mid(user),
        actor_user_id=user.user_id,
    )


@router.post("/{dispute_id}/contest")
async def contest_dispute(
    dispute_id: int,
    body: _ContestBody,
    user: UserContext = Depends(require_permission("razorpay.disputes.write")),
):
    """
    Submit/draft evidence for a Razorpay dispute. action='submit' finalises
    and sends to the network (transitions local row to 'evidence_submitted').
    """
    return await dispute_service.contest_via_gateway(
        dispute_id,
        merchant_id=_mid(user),
        evidence=body.evidence,
        action=body.action,
        actor_user_id=user.user_id,
    )


@router.post("/{dispute_id}/sync")
async def sync_dispute(
    dispute_id: int,
    user: UserContext = Depends(require_permission("razorpay.disputes.read")),
):
    """Re-fetch a dispute from Razorpay and reconcile the local row."""
    return await dispute_service.sync_from_gateway(
        dispute_id, merchant_id=_mid(user),
    )
