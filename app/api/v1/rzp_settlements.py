"""
Razorpay settlements REST API (Phase 6 — settlements deep-wire).

Merchant-facing routes that read the locally-mirrored Razorpay settlement
state and trigger on-demand recon pulls. All writes that hit the gateway
go through `rzp_settlement_service` so they share the same idempotency and
merchant-resolution rules as the webhook back-flow.

Prefix: ``/razorpay-settlements`` — deliberately distinct from the legacy
``/settlements`` router (which is gateway-agnostic and predates Phase 6).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.logging import get_logger
from app.services.razorpay.settlement_service import rzp_settlement_service

logger = get_logger(__name__)

router = APIRouter(prefix="/razorpay-settlements", tags=["Razorpay Settlements"])


def _mid(user: UserContext) -> str:
    if not user.restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    return str(user.restaurant_id)


# ── Models ────────────────────────────────────────────────────────────────


class ReconSyncIn(BaseModel):
    year: int = Field(..., ge=2020, le=2100)
    month: int = Field(..., ge=1, le=12)
    day: Optional[int] = Field(None, ge=1, le=31)


# ── Routes ────────────────────────────────────────────────────────────────


@router.get("")
async def list_settlements(
    status: Optional[str] = Query(None, description="pending|processing|processed|failed|reversed"),
    from_ts: Optional[datetime] = Query(None),
    to_ts: Optional[datetime] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("razorpay.settlements.read")),
):
    """List Razorpay settlements scoped to the current merchant."""
    return await rzp_settlement_service.list_settlements(
        merchant_id=_mid(user),
        status=status,
        from_ts=from_ts,
        to_ts=to_ts,
        limit=limit,
        offset=offset,
    )


@router.get("/{settlement_id}")
async def get_settlement(
    settlement_id: str,
    user: UserContext = Depends(require_permission("razorpay.settlements.read")),
):
    row = await rzp_settlement_service.get_settlement(
        settlement_id, merchant_id=_mid(user),
    )
    if not row:
        raise HTTPException(status_code=404, detail="settlement not found")
    return row


@router.get("/{settlement_id}/payments")
async def list_settlement_payments(
    settlement_id: str,
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("razorpay.settlements.read")),
):
    """Per-payment breakdown sourced from the recon report."""
    row = await rzp_settlement_service.get_settlement(
        settlement_id, merchant_id=_mid(user),
    )
    if not row:
        raise HTTPException(status_code=404, detail="settlement not found")
    return await rzp_settlement_service.list_settlement_payments(
        settlement_id, merchant_id=_mid(user),
        limit=limit, offset=offset,
    )


@router.post("/{settlement_id}/sync")
async def sync_settlement(
    settlement_id: str,
    user: UserContext = Depends(require_permission("razorpay.settlements.read")),
):
    """Refetch a single settlement from Razorpay and re-mirror locally."""
    return await rzp_settlement_service.sync_settlement(
        settlement_id, merchant_id=_mid(user),
    )


@router.post("/recon/sync")
async def sync_recon(
    body: ReconSyncIn,
    user: UserContext = Depends(require_permission("razorpay.recon.run")),
):
    """
    Pull the recon report for a calendar period and persist every row.

    Heavy-ish operation — typically scheduled, but exposed here so a
    merchant can self-serve a backfill if a webhook was missed.
    """
    # Reject obvious future dates so we don't burn API calls.
    today = datetime.now(timezone.utc).date()
    if (body.year, body.month) > (today.year, today.month):
        raise HTTPException(status_code=400, detail="cannot recon future months")
    return await rzp_settlement_service.fetch_recon_and_persist(
        year=body.year, month=body.month, day=body.day,
        merchant_id=_mid(user),
    )
