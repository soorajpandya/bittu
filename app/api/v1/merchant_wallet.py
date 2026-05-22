"""
Merchant Wallet API — Razorpay-backed.

Single-pane-of-glass for merchants. All numbers are projected live from
Razorpay using the merchant's linked Route account, with Bittu's flat 5%
commission split applied at presentation time.

  GET /merchant-wallet                              Wallet snapshot
  GET /merchant-wallet/transactions                 Payment ledger (filterable)
  GET /merchant-wallet/settlements                  Settlement batches
  GET /merchant-wallet/settlements/{id}             Settlement detail
  GET /merchant-wallet/settlements/{id}/timeline    State transitions
  GET /merchant-wallet/export                       CSV statement

All endpoints are scoped to the caller's `restaurant_id` (= rzp linked account).
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.core.auth import UserContext, require_permission
from app.core.cache import cached_route
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.services.razorpay.merchant_finance_service import merchant_finance_service

router = APIRouter(prefix="/merchant-wallet", tags=["Merchant Ledger"])
logger = get_logger(__name__)


def _merchant_id(user: UserContext) -> str:
    mid = user.restaurant_id
    if not mid:
        raise ValidationError("No restaurant is bound to this user context.")
    return mid


# ── Wallet snapshot ────────────────────────────────────────────────────
@router.get("")
@router.get("/")
async def wallet(
    from_date: Optional[date] = Query(None, description="Start of window (UTC, inclusive)"),
    to_date:   Optional[date] = Query(None, description="End of window (UTC, inclusive)"),
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    """Live Razorpay-derived wallet snapshot with 5% commission applied."""
    return await _cached_wallet(from_date, to_date, user)


@cached_route(prefix="merchant_wallet_v2", ttl=30)
async def _cached_wallet(from_date, to_date, user: UserContext):
    return await merchant_finance_service.wallet_snapshot(
        _merchant_id(user), from_date=from_date, to_date=to_date,
    )


# ── Transactions ───────────────────────────────────────────────────────
@router.get("/transactions")
async def transactions(
    payment_method: Optional[str]   = Query(None, description="upi / card / netbanking / wallet"),
    min_amount:     Optional[float] = Query(None, ge=0),
    max_amount:     Optional[float] = Query(None, ge=0),
    search:         Optional[str]   = Query(None, description="payment_id / order_id / email / phone / vpa"),
    from_date:      Optional[date]  = Query(None),
    to_date:        Optional[date]  = Query(None),
    limit:          int = Query(50, ge=1, le=500),
    offset:         int = Query(0,  ge=0),
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    """Captured payments for the merchant (live Razorpay)."""
    return await _cached_transactions(
        payment_method, min_amount, max_amount, search,
        from_date, to_date, limit, offset, user,
    )


@cached_route(prefix="merchant_wallet_v2_tx", ttl=30)
async def _cached_transactions(
    payment_method, min_amount, max_amount, search,
    from_date, to_date, limit, offset, user: UserContext,
):
    return await merchant_finance_service.list_transactions(
        _merchant_id(user),
        from_date=from_date, to_date=to_date,
        payment_method=payment_method,
        min_amount=min_amount, max_amount=max_amount,
        search=search, limit=limit, offset=offset,
    )


# ── Settlements ────────────────────────────────────────────────────────
@router.get("/settlements")
async def settlements(
    status:    Optional[str]  = Query(None, description="created / processed / failed"),
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    limit:     int = Query(50, ge=1, le=500),
    offset:    int = Query(0,  ge=0),
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    return await _cached_settlements(status, from_date, to_date, limit, offset, user)


@cached_route(prefix="merchant_wallet_v2_setl", ttl=60)
async def _cached_settlements(status, from_date, to_date, limit, offset, user: UserContext):
    return await merchant_finance_service.list_settlements(
        _merchant_id(user),
        status=status, from_date=from_date, to_date=to_date,
        limit=limit, offset=offset,
    )


@router.get("/settlements/{settlement_id}")
async def settlement_detail(
    settlement_id: str,
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    return await _cached_settlement_detail(settlement_id, user)


@cached_route(prefix="merchant_wallet_v2_setl_detail", ttl=60)
async def _cached_settlement_detail(settlement_id, user: UserContext):
    return await merchant_finance_service.get_settlement(_merchant_id(user), settlement_id)


@router.get("/settlements/{settlement_id}/timeline")
async def settlement_timeline(
    settlement_id: str,
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    return await _cached_settlement_timeline(settlement_id, user)


@cached_route(prefix="merchant_wallet_v2_setl_timeline", ttl=60)
async def _cached_settlement_timeline(settlement_id, user: UserContext):
    return await merchant_finance_service.settlement_timeline(_merchant_id(user), settlement_id)


# ── CSV export ─────────────────────────────────────────────────────────
@router.get("/export")
async def export_csv(
    from_date: Optional[date] = Query(None, description="Defaults to T-30 days"),
    to_date:   Optional[date] = Query(None, description="Defaults to today"),
    user: UserContext = Depends(require_permission("statements.export")),
):
    """Download a Razorpay-reconciled CSV statement (live)."""
    csv_text, filename = await merchant_finance_service.export_csv(
        _merchant_id(user), from_date=from_date, to_date=to_date,
    )
    return StreamingResponse(
        iter([csv_text]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
