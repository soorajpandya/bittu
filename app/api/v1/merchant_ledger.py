"""
Merchant Ledger API — Razorpay-derived (read-only).

The merchant-facing ledger is now a synthetic projection over the live
Razorpay state (payments → CREDIT, settlements → DEBIT, refunds → DEBIT,
plus a virtual DEBIT for Bittu's 5% commission on every captured payment).

  GET  /merchant-ledger/balance              Current running balance
  GET  /merchant-ledger/entries              Paginated history
  GET  /merchant-ledger/entries/{entry_id}   Single entry (entry_id =
                                             "pay:<id>" / "setl:<id>" /
                                             "com:<id>" / "ref:<id>")
  GET  /merchant-ledger/consistency-check    Verify projection invariants
  POST /merchant-ledger/consistency-check    (alias)
  POST /merchant-ledger/manual-adjustment    410 Gone — the ledger is now
                                             derived; adjust the underlying
                                             Razorpay entity instead.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import UserContext, require_permission
from app.core.cache import cached_route
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.services.razorpay.merchant_finance_service import merchant_finance_service

router = APIRouter(prefix="/merchant-ledger", tags=["Merchant Ledger"])
logger = get_logger(__name__)


def _merchant_id(user: UserContext) -> str:
    mid = user.restaurant_id
    if not mid:
        raise ValidationError("No restaurant is bound to this user context.")
    return mid


# ── Balance ────────────────────────────────────────────────────────────
@router.get("/balance")
async def get_balance(
    user: UserContext = Depends(require_permission("merchant_ledger.read")),
):
    return await _cached_balance(user)


@cached_route(prefix="merchant_ledger_v2_balance", ttl=30)
async def _cached_balance(user: UserContext):
    return await merchant_finance_service.ledger_balance(_merchant_id(user))


# ── Entries ────────────────────────────────────────────────────────────
@router.get("/entries")
async def list_entries(
    entry_type: Optional[str] = Query(None, description="CREDIT or DEBIT"),
    source:     Optional[str] = Query(None, description="payment, settlement, commission, refund"),
    from_date:  Optional[date] = Query(None),
    to_date:    Optional[date] = Query(None),
    limit:      int = Query(50, ge=1, le=200),
    offset:     int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("merchant_ledger.read")),
):
    return await _cached_entries(entry_type, source, from_date, to_date, limit, offset, user)


@cached_route(prefix="merchant_ledger_v2_entries", ttl=30)
async def _cached_entries(entry_type, source, from_date, to_date, limit, offset, user: UserContext):
    return await merchant_finance_service.list_ledger_entries(
        _merchant_id(user),
        entry_type=entry_type, source=source,
        from_date=from_date, to_date=to_date,
        limit=limit, offset=offset,
    )


# ── Single entry ───────────────────────────────────────────────────────
@router.get("/entries/{entry_id}")
async def get_entry(
    entry_id: str,
    user: UserContext = Depends(require_permission("merchant_ledger.read")),
):
    return await merchant_finance_service.get_ledger_entry(_merchant_id(user), entry_id)


# ── Consistency check ──────────────────────────────────────────────────
@router.post("/consistency-check")
@router.get("/consistency-check")
async def consistency_check(
    user: UserContext = Depends(require_permission("merchant_ledger.admin")),
):
    """Recompute the projection from live Razorpay state and verify invariants."""
    return await merchant_finance_service.ledger_consistency_check(_merchant_id(user))


# ── Manual adjustment — DEPRECATED ─────────────────────────────────────
@router.post("/manual-adjustment")
async def manual_adjustment(
    user: UserContext = Depends(require_permission("merchant_ledger.admin")),
):
    """
    410 Gone. The merchant ledger is now projected live from Razorpay and
    is no longer a writable store. Adjustments must be made against the
    underlying Razorpay entity (refund a payment, dispute a settlement,
    etc.) and they will surface here automatically.
    """
    raise HTTPException(
        status_code=410,
        detail=(
            "Manual ledger adjustments are disabled. The merchant ledger is "
            "now derived live from Razorpay; mutate the underlying Razorpay "
            "entity instead."
        ),
    )
