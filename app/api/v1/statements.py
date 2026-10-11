"""
Statement & Settlement API
═══════════════════════════════════════════════════════════════════════════════
Merchant-facing settlement experience (Sidebar → Finance → Statement).

Endpoints:
  GET  /statements/summary                  Dashboard summary (7 KPIs + ETA)
  GET  /statements/transactions             Paginated transaction list
  GET  /statements/settlements              Paginated settlement batch list
  GET  /statements/settlements/{id}         Settlement detail + timeline
  GET  /statements/pending                  Pending/processing settlements
  GET  /statements/timeline/{settlement_id} Full timeline for a settlement
  GET  /statements/export                   Structured export (PDF/Excel data)
  GET  /statements/fee-calculator           Preview fee for a gross amount
  POST /statements/settlements/{id}/advance (admin) Advance settlement status
  POST /statements/payments/{pid}/enqueue   (internal) Enqueue payment
═══════════════════════════════════════════════════════════════════════════════
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.cache import cached_route
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.services.statement_service import statement_service

router = APIRouter(prefix="/statements", tags=["Settlements"])
logger = get_logger(__name__)


# ── Helper ────────────────────────────────────────────────────────────────────

def _rid(user: UserContext) -> str:
    return user.restaurant_id


def _bid(user: UserContext) -> Optional[str]:
    return user.branch_id if user.is_branch_user else None


def _handle_not_found(exc: NotFoundError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


def _handle_validation(exc: ValidationError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


# ── Request / Response models ─────────────────────────────────────────────────

class AdvanceSettlementRequest(BaseModel):
    new_status: str = Field(
        ...,
        description="Target status: processing | sent_to_bank | settled | failed | reversed",
    )
    bank_reference_number: Optional[str] = None
    failure_reason: Optional[str] = None
    metadata: Optional[dict] = None


class EnqueuePaymentRequest(BaseModel):
    order_id: str
    restaurant_id: Optional[str] = None
    branch_id: Optional[str] = None
    gross_amount: float = Field(..., gt=0)
    payment_method: str = "upi"
    customer_name: Optional[str] = None
    order_reference: Optional[str] = None
    cycle: str = Field("T+1", pattern="^T\\+[01]$")


# ══════════════════════════════════════════════════════════════════════════════
# 1. SUMMARY DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/summary",
    summary="Statement summary dashboard",
    description=(
        "Returns the top-level merchant dashboard:\n"
        "- Total received, today's collection\n"
        "- Pending settlement, settled amount\n"
        "- Bittu platform charges, GST on charges\n"
        "- Net amount credited to bank\n"
        "- Upcoming settlement ETA with human-readable message\n\n"
        "Response optimised for the sticky summary cards on mobile."
    ),
)
async def get_summary(
    branch_id: Optional[str] = Query(None, description="Filter by branch (auto-set for branch staff)"),
    from_date: Optional[date] = Query(None, description="Start date (default: 1st of current month)"),
    to_date: Optional[date]   = Query(None, description="End date (default: today)"),
    user: UserContext = Depends(require_permission("statements.read")),
):
    return await _cached_summary(branch_id, from_date, to_date, user)


@cached_route(prefix="statements", ttl=30)
async def _cached_summary(
    branch_id: Optional[str],
    from_date: Optional[date],
    to_date: Optional[date],
    user: UserContext,
):
    restaurant_id = _rid(user)
    effective_branch = _bid(user) or branch_id
    try:
        return await statement_service.get_summary(
            restaurant_id=restaurant_id,
            branch_id=effective_branch,
            from_date=from_date,
            to_date=to_date,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("statement_summary_failed", error=str(exc), user_id=user.user_id)
        raise HTTPException(status_code=500, detail="Failed to load statement summary")


# ══════════════════════════════════════════════════════════════════════════════
# 2. TRANSACTIONS LIST
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/transactions",
    summary="Paginated transaction list",
    description=(
        "Each row shows: Order ID, Payment ID, Customer, Amount, Bittu Fee, "
        "GST, Net Amount, Payment Method, Settlement Status, Created Time, ETA.\n\n"
        "Supports: search, filters, branch filter, date range, status filter, sorting, pagination."
    ),
)
async def get_transactions(
    branch_id:          Optional[str]  = Query(None),
    from_date:          Optional[date] = Query(None),
    to_date:            Optional[date] = Query(None),
    settlement_status:  Optional[str]  = Query(None, description="pending|processing|sent_to_bank|settled|failed|reversed"),
    payment_method:     Optional[str]  = Query(None, description="cash|upi|card|wallet|online"),
    search:             Optional[str]  = Query(None, description="Search by customer name, order ref, or payment ID"),
    sort_by:            str            = Query("created_at", description="created_at|gross_amount|net_amount|payment_method"),
    sort_dir:           str            = Query("desc",       description="asc|desc"),
    limit:              int            = Query(50,  ge=1, le=200),
    offset:             int            = Query(0,   ge=0),
    user: UserContext = Depends(require_permission("statements.read")),
):
    restaurant_id     = _rid(user)
    effective_branch  = _bid(user) or branch_id
    try:
        return await statement_service.get_transactions(
            restaurant_id=restaurant_id,
            branch_id=effective_branch,
            from_date=from_date,
            to_date=to_date,
            settlement_status=settlement_status,
            payment_method=payment_method,
            search=search,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("statement_transactions_failed", error=str(exc), user_id=user.user_id)
        raise HTTPException(status_code=500, detail="Failed to load transactions")


# ══════════════════════════════════════════════════════════════════════════════
# 3. SETTLEMENTS LIST
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/settlements",
    summary="Paginated settlement batch list",
    description=(
        "Lists Razorpay settlement mirrors with status, amounts, and reconciliation count.\n"
        "Filter by status and date range. Settlement cycles are managed by Razorpay."
    ),
)
async def list_settlements(
    branch_id:  Optional[str]  = Query(None),
    from_date:  Optional[date] = Query(None),
    to_date:    Optional[date] = Query(None),
    status:     Optional[str]  = Query(None, description="pending|processing|sent_to_bank|settled|failed|reversed"),
    cycle:      Optional[str]  = Query(None, description="T+0 or T+1"),
    sort_by:    str            = Query("created_at", description="created_at|gross_amount|net_settlement_amount|expected_settlement_at"),
    sort_dir:   str            = Query("desc"),
    limit:      int            = Query(50, ge=1, le=200),
    offset:     int            = Query(0,  ge=0),
    user: UserContext = Depends(require_permission("statements.read")),
):
    restaurant_id    = _rid(user)
    effective_branch = _bid(user) or branch_id
    try:
        return await statement_service.get_settlements(
            restaurant_id=restaurant_id,
            branch_id=effective_branch,
            from_date=from_date,
            to_date=to_date,
            status=status,
            cycle=cycle,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("list_settlements_failed", error=str(exc), user_id=user.user_id)
        raise HTTPException(status_code=500, detail="Failed to load settlements")


# ══════════════════════════════════════════════════════════════════════════════
# 4. SETTLEMENT DETAIL
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/settlements/{settlement_id}",
    summary="Settlement detail with breakdown and timeline",
    description=(
        "Full detail for a single settlement batch:\n"
        "- Settlement metadata + status\n"
        "- Fee breakdown with formula string\n"
        "- All included transactions\n"
        "- Observed current status (prior transition history may be unavailable)\n"
        "- Bank reference number (when available)\n"
        "- Reconciliation availability"
    ),
)
async def get_settlement_detail(
    settlement_id: str,
    user: UserContext = Depends(require_permission("statements.settlement.read")),
):
    restaurant_id = _rid(user)
    try:
        return await statement_service.get_settlement_detail(
            settlement_id=settlement_id,
            restaurant_id=restaurant_id,
            branch_id=_bid(user),
        )
    except NotFoundError as exc:
        raise _handle_not_found(exc)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("settlement_detail_failed", settlement_id=settlement_id, error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to load settlement detail")


# ══════════════════════════════════════════════════════════════════════════════
# 5. PENDING SETTLEMENTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/pending",
    summary="Pending and in-flight settlements",
    description=(
        "All settlements in pending / processing / sent-to-bank state "
        "using gateway-owned settlement timing.\n\n"
        "Designed for the 'Pending' tab on mobile — fast and minimal."
    ),
)
async def get_pending(
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("statements.read")),
):
    restaurant_id    = _rid(user)
    effective_branch = _bid(user) or branch_id
    try:
        return await statement_service.get_pending(
            restaurant_id=restaurant_id,
            branch_id=effective_branch,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("pending_settlements_failed", error=str(exc), user_id=user.user_id)
        raise HTTPException(status_code=500, detail="Failed to load pending settlements")


# ══════════════════════════════════════════════════════════════════════════════
# 6. SETTLEMENT TIMELINE
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/timeline/{settlement_id}",
    summary="Observed Razorpay settlement state",
    description=(
        "Returns the current observed mirror state and its known timestamp. "
        "The response marks history_complete=false when prior transitions are unavailable."
    ),
)
async def get_settlement_timeline(
    settlement_id: str,
    user: UserContext = Depends(require_permission("statements.read")),
):
    restaurant_id = _rid(user)
    try:
        return await statement_service.get_settlement_timeline(
            settlement_id=settlement_id,
            restaurant_id=restaurant_id,
            branch_id=_bid(user),
        )
    except NotFoundError as exc:
        raise _handle_not_found(exc)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("timeline_failed", settlement_id=settlement_id, error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to load settlement timeline")


# ══════════════════════════════════════════════════════════════════════════════
# 7. EXPORT
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/export",
    summary="Statement export (PDF/Excel data)",
    description=(
        "Returns structured data for PDF or Excel generation on the frontend:\n"
        "- Export metadata (restaurant name, period, generated timestamp)\n"
        "- Summary totals (gross, fees, GST, net settled, pending)\n"
        "- Full settlement list with per-settlement breakdown\n"
        "- Full transaction list with all columns\n"
        "- Column headers for table rendering\n\n"
        "Supports filtering by date range, branch, and settlement status."
    ),
)
async def export_statement(
    branch_id:         Optional[str]  = Query(None),
    from_date:         Optional[date] = Query(None, description="Default: 1st of current month"),
    to_date:           Optional[date] = Query(None, description="Default: today"),
    settlement_status: Optional[str]  = Query(None, description="Filter by status"),
    user: UserContext = Depends(require_permission("statements.export")),
):
    restaurant_id    = _rid(user)
    effective_branch = _bid(user) or branch_id
    try:
        return await statement_service.export_statement(
            restaurant_id=restaurant_id,
            branch_id=effective_branch,
            from_date=from_date,
            to_date=to_date,
            settlement_status=settlement_status,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("statement_export_failed", error=str(exc), user_id=user.user_id)
        raise HTTPException(status_code=500, detail="Failed to generate statement export")


# ══════════════════════════════════════════════════════════════════════════════
# 8. FEE CALCULATOR
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/debug-context",
    summary="[Temp] Show resolved user context and payment count",
    include_in_schema=False,
)
async def debug_context(
    user: UserContext = Depends(require_permission("statements.read")),
):
    """Temporary debug endpoint — remove after diagnosis."""
    from app.core.database import get_connection
    rid = _rid(user)
    bid = _bid(user)
    result = {"user_id": user.user_id, "restaurant_id": rid, "branch_id": bid,
              "is_branch_user": user.is_branch_user, "role": user.role}
    try:
        async with get_connection() as conn:
            counts = await conn.fetch(
                "SELECT status, COUNT(*) AS cnt FROM payments WHERE restaurant_id = $1::uuid GROUP BY status",
                rid,
            ) if rid else []
            result["payments_by_status"] = {r["status"]: r["cnt"] for r in counts}
            total = await conn.fetchval("SELECT COUNT(*) FROM payments WHERE restaurant_id = $1::uuid", rid) if rid else 0
            result["total_payments"] = total
    except HTTPException:
        raise
    except Exception as exc:
        result["db_error"] = str(exc)
    return result


@router.get(
    "/fee-calculator",
    summary="Preview the per-settlement deduction breakdown for a gross amount",
    description=(
        "Returns the real-time fee breakdown under the 5 % gross-deduction model:\n"
        "  gross × 5.00 %    = total deduction\n"
        "  gateway estimate = environment-configured preview; actual fee comes from Razorpay\n"
        "  remainder ÷ 1.18  = Bittu platform fee (reverse-GST extracted)\n"
        "  remainder − fee   = GST on fee (residual, paisa-exact)\n"
        "  net = gross − total deduction (95 % of gross)\n\n"
        "Useful for the frontend to show merchants exactly what they will receive."
    ),
)
async def fee_calculator(
    amount: float = Query(..., gt=0, description="Gross amount to calculate fee for"),
    method: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("statements.read")),
):
    return statement_service.calculate_fee(amount, method)


# ══════════════════════════════════════════════════════════════════════════════
# 9. ADMIN: ADVANCE SETTLEMENT STATUS
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/settlements/{settlement_id}/advance",
    summary="[Admin] Advance settlement to next status",
    description=(
        "Advances a settlement through its lifecycle:\n"
        "  pending → processing → sent_to_bank → settled | failed\n\n"
        "Retired: manual status changes return 410.\n"
        "Requires `statements.admin` permission."
    ),
)
async def advance_settlement(
    settlement_id: str,
    body: AdvanceSettlementRequest = None,  # type: ignore[assignment]
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
    user: UserContext = Depends(require_permission("statements.admin")),
):
    """
    410 Gone. Settlement mirrors are updated by Razorpay webhooks and follow
    Razorpay's own lifecycle (`created → processing → processed | failed`).
    There is no manually-advanceable state on the Bittu side anymore.
    """
    raise HTTPException(
        status_code=410,
        detail=(
            "Manual settlement advancement is disabled. Settlement state is "
            "now owned by Razorpay; use GET /merchant-wallet/settlements/"
            f"{settlement_id}/timeline to inspect the live lifecycle."
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# 10. INTERNAL: ENQUEUE PAYMENT FOR SETTLEMENT
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/payments/{payment_id}/enqueue",
    status_code=201,
    summary="[Internal] Enqueue a completed payment for settlement",
    description=(
        "Called internally by the payment service when a payment completes.\n"
        "Retired: returns 410. Route transfers happen on capture.\n"
        "Idempotent: duplicate calls for the same payment_id are safe (returns existing)."
    ),
)
async def enqueue_payment(
    payment_id: str,
    body: EnqueuePaymentRequest,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
    user: UserContext = Depends(require_permission("statements.admin")),
):
    raise HTTPException(status_code=410, detail="Legacy settlement enqueue is retired; Razorpay Route owns settlement.")
