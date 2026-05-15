"""
Merchant Ledger API — read-side inspection of the immutable ledger.

  GET /merchant-ledger/balance              Current running balance
  GET /merchant-ledger/entries              Paginated, filterable history
  GET /merchant-ledger/entries/{entry_id}   Single entry detail
  GET /merchant-ledger/consistency-check    Recompute balance vs running

Posting to the ledger is intentionally NOT exposed as a public HTTP endpoint
in Phase 1 — money movements happen via the existing payment / settlement
flows, which (in subsequent phases) will be wired to call
`merchant_ledger_service.post_entry(...)` directly.

A guarded admin-only `POST /merchant-ledger/manual-adjustment` endpoint is
provided for back-office corrections; it requires `merchant_ledger.admin`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.logging import get_logger
from app.services.merchant_ledger_service import merchant_ledger_service

router = APIRouter(prefix="/merchant-ledger", tags=["Merchant Ledger"])
logger = get_logger(__name__)


# ── Balance ────────────────────────────────────────────────────────────
@router.get("/balance")
async def get_balance(
    currency: str = Query("INR", min_length=3, max_length=3),
    user: UserContext = Depends(require_permission("merchant_ledger.read")),
):
    """Current running balance for the caller's merchant + currency."""
    return await merchant_ledger_service.get_balance(user, currency=currency)


# ── List entries ───────────────────────────────────────────────────────
@router.get("/entries")
async def list_entries(
    transaction_type: Optional[str] = Query(None, description="Filter by txn type"),
    settlement_id:    Optional[str] = Query(None),
    payment_id:       Optional[str] = Query(None),
    order_id:         Optional[str] = Query(None),
    utr_number:       Optional[str] = Query(None),
    from_date:        Optional[datetime] = Query(None, description="ISO8601 inclusive"),
    to_date:          Optional[datetime] = Query(None, description="ISO8601 inclusive"),
    currency:         str = Query("INR", min_length=3, max_length=3),
    limit:            int = Query(50, ge=1, le=200),
    cursor:           Optional[str] = Query(None, description="Opaque keyset cursor"),
    user: UserContext = Depends(require_permission("merchant_ledger.read")),
):
    """Paginated, filterable ledger history (DESC by created_at, id)."""
    return await merchant_ledger_service.list_entries(
        user,
        transaction_type=transaction_type,
        settlement_id=settlement_id,
        payment_id=payment_id,
        order_id=order_id,
        utr_number=utr_number,
        from_date=from_date.isoformat() if from_date else None,
        to_date=to_date.isoformat() if to_date else None,
        currency=currency,
        limit=limit,
        cursor=cursor,
    )


# ── Single entry ───────────────────────────────────────────────────────
@router.get("/entries/{entry_id}")
async def get_entry(
    entry_id: str,
    user: UserContext = Depends(require_permission("merchant_ledger.read")),
):
    return await merchant_ledger_service.get_entry(user, entry_id)


# ── Consistency check ──────────────────────────────────────────────────
# POST is the canonical verb (it recomputes/asserts state). GET is kept
# as a safe alias because it has no side effects beyond reading.
@router.post("/consistency-check")
@router.get("/consistency-check")
async def consistency_check(
    currency: str = Query("INR", min_length=3, max_length=3),
    user: UserContext = Depends(require_permission("merchant_ledger.admin")),
):
    """
    Recomputes balance from the sum of all credit-debit movements and
    compares against the running balance lock + the latest entry's
    `balance_after`. Both must match; mismatch is a critical alert.
    """
    return await merchant_ledger_service.verify_consistency(user, currency=currency)


# ── Manual adjustment (admin only) ─────────────────────────────────────
class ManualAdjustmentBody(BaseModel):
    transaction_type: str = Field(
        ...,
        description="One of: adjustment, manual_credit, manual_debit, "
                    "reserve_hold, reserve_release",
    )
    amount: float = Field(..., gt=0)
    direction: str = Field(..., description="'credit' or 'debit'")
    currency: str = Field("INR", min_length=3, max_length=3)
    reason: str = Field(..., min_length=3, max_length=500)
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    metadata: dict = Field(default_factory=dict)


@router.post("/manual-adjustment")
async def manual_adjustment(
    body: ManualAdjustmentBody = Body(...),
    user: UserContext = Depends(require_permission("merchant_ledger.admin")),
):
    """
    Post a manual ledger adjustment. Requires `merchant_ledger.admin`.

    Idempotent: re-posting with the same `idempotency_key` returns the
    original entry instead of creating a duplicate.
    """
    if body.direction not in ("credit", "debit"):
        from app.core.exceptions import ValidationError
        raise ValidationError("direction must be 'credit' or 'debit'")

    metadata = {
        **(body.metadata or {}),
        "reason":     body.reason,
        "actor_id":   user.user_id,
        "actor_role": user.role,
    }
    return await merchant_ledger_service.post_entry(
        merchant_id=user.restaurant_id,
        branch_id=user.branch_id,
        transaction_type=body.transaction_type,
        debit_amount=body.amount  if body.direction == "debit"  else 0,
        credit_amount=body.amount if body.direction == "credit" else 0,
        currency=body.currency,
        source_type="manual_adjustment",
        idempotency_key=body.idempotency_key,
        metadata=metadata,
        created_by=user.user_id,
    )
