"""
Merchant Wallet API.

Single-pane-of-glass for merchants:
  GET /merchant-wallet                      Wallet snapshot (cash + online + platform revenue)
  GET /merchant-wallet/fee-quote            Pure-function fee preview
  GET /merchant-wallet/transactions         Unified payment ledger (paginated, filterable)
  GET /merchant-wallet/settlements          Settlement batch history
  GET /merchant-wallet/settlements/{id}     Settlement detail (txns + timeline)
  GET /merchant-wallet/daily-closing        One-day close report
  GET /merchant-wallet/platform-revenue     Bittu's revenue + GST collected (period)
  GET /merchant-wallet/gst-on-fee           GST liability on platform fee (for ITC)

All endpoints are scoped to the caller's restaurant_id.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.core.auth import UserContext, require_permission
from app.core.cache import cached_route
from app.core.logging import get_logger
from app.services.merchant_wallet_service import merchant_wallet_service

router = APIRouter(prefix="/merchant-wallet", tags=["Merchant Wallet"])
logger = get_logger(__name__)


# ── Wallet snapshot ────────────────────────────────────────────────────
@router.get("")
@router.get("/")
async def wallet(
    as_of_date: Optional[date] = Query(
        None,
        description="Snapshot as of end of this date (UTC). Defaults to live/now.",
    ),
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    """All balances + lifetime stats in a single payload."""
    return await _cached_wallet(as_of_date, user)


# Historical snapshots are immutable, so we can cache them for a long time.
# Live snapshots get a short TTL so realtime mutations show up quickly.
@cached_route(prefix="merchant_wallet", ttl=15)
async def _cached_wallet_live(as_of_date, user: UserContext):
    return await merchant_wallet_service.wallet(user, as_of_date=as_of_date)


@cached_route(prefix="merchant_wallet_hist", ttl=86400)
async def _cached_wallet_hist(as_of_date, user: UserContext):
    return await merchant_wallet_service.wallet(user, as_of_date=as_of_date)


async def _cached_wallet(as_of_date, user: UserContext):
    if as_of_date is None:
        return await _cached_wallet_live(as_of_date, user)
    return await _cached_wallet_hist(as_of_date, user)


# ── Fee preview ────────────────────────────────────────────────────────
@router.get("/fee-quote")
async def fee_quote(
    gross_amount: float = Query(..., gt=0, description="Pre-fee transaction amount"),
    method:       str   = Query("upi", description="upi, card, cash, etc."),
    user: UserContext   = Depends(require_permission("bank_recon.read")),
):
    """Preview the platform fee, GST and net settlement for a transaction."""
    return await merchant_wallet_service.quote_fee(gross_amount, method)


# ── Transaction ledger ─────────────────────────────────────────────────
@router.get("/transactions")
async def transactions(
    method:    Optional[str]  = Query(None, description="cash | online | <method-name>"),
    status:    Optional[str]  = Query(None, description="completed, refunded, pending, ..."),
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    limit:     int = Query(50, ge=1, le=200),
    offset:    int = Query(0,  ge=0),
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    """Unified payment ledger (joined with settlement lifecycle)."""
    return await merchant_wallet_service.list_transactions(
        user,
        method=method, status=status,
        from_date=from_date, to_date=to_date,
        limit=limit, offset=offset,
    )


# ── Settlement history ─────────────────────────────────────────────────
@router.get("/settlements")
async def settlements(
    status:    Optional[str]  = Query(None, description="pending, processing, sent_to_bank, settled, failed, reversed"),
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    limit:     int = Query(50, ge=1, le=200),
    offset:    int = Query(0,  ge=0),
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    """List of settlement batches (Bittu → merchant bank)."""
    return await merchant_wallet_service.list_settlements(
        user,
        status=status,
        from_date=from_date, to_date=to_date,
        limit=limit, offset=offset,
    )


@router.get("/settlements/{settlement_id}")
async def settlement_detail(
    settlement_id: str,
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    """Header + individual transaction lines + state transition timeline."""
    return await merchant_wallet_service.get_settlement(user, settlement_id)


# ── Daily closing ──────────────────────────────────────────────────────
@router.get("/daily-closing")
async def daily_closing(
    closing_date: Optional[date] = Query(None, description="Defaults to today (UTC)"),
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    """Single-day close: cash, online captured, online settled, fees, GST, refunds."""
    return await merchant_wallet_service.daily_closing(user, closing_date)


# ── Reports ────────────────────────────────────────────────────────────
@router.get("/platform-revenue")
async def platform_revenue(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    """Bittu's fee + GST collected from this merchant for the period."""
    return await merchant_wallet_service.platform_revenue_report(
        user, from_date=from_date, to_date=to_date
    )


@router.get("/gst-on-fee")
async def gst_on_fee(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    """
    GST charged by Bittu on the platform fee, day-by-day.
    Merchants use this to claim Input Tax Credit (ITC).
    """
    return await merchant_wallet_service.fee_gst_report(
        user, from_date=from_date, to_date=to_date
    )
