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

from fastapi import APIRouter, Depends, Query, Header, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.services.statement_service import statement_service

router = APIRouter(prefix="/statements", tags=["Statement & Settlement"])
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
    restaurant_id = _rid(user)
    effective_branch = branch_id or _bid(user)
    try:
        return await statement_service.get_summary(
            restaurant_id=restaurant_id,
            branch_id=effective_branch,
            from_date=from_date,
            to_date=to_date,
        )
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
    effective_branch  = branch_id or _bid(user)
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
        "Lists all Bittu settlement batches with status, amounts, and transaction count.\n"
        "Filter by status, date range, or settlement cycle (T+0 / T+1)."
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
    effective_branch = branch_id or _bid(user)
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
        "- Complete status timeline\n"
        "- Bank reference number (when available)\n"
        "- Retry history + failure reasons"
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
        )
    except NotFoundError as exc:
        raise _handle_not_found(exc)
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
        "with their expected settlement ETA.\n\n"
        "Designed for the 'Pending' tab on mobile — fast and minimal."
    ),
)
async def get_pending(
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("statements.read")),
):
    restaurant_id    = _rid(user)
    effective_branch = branch_id or _bid(user)
    try:
        return await statement_service.get_pending(
            restaurant_id=restaurant_id,
            branch_id=effective_branch,
        )
    except Exception as exc:
        logger.error("pending_settlements_failed", error=str(exc), user_id=user.user_id)
        raise HTTPException(status_code=500, detail="Failed to load pending settlements")


# ══════════════════════════════════════════════════════════════════════════════
# 6. SETTLEMENT TIMELINE
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/timeline/{settlement_id}",
    summary="Immutable event timeline for a settlement",
    description=(
        "Returns ordered list of all status transitions and events for a settlement:\n"
        "- created → processing_started → bank_transfer_initiated → settled/failed\n"
        "- Each event carries timestamp, actor, and structured metadata\n"
        "- Timeline rows are immutable (append-only audit log)"
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
        )
    except NotFoundError as exc:
        raise _handle_not_found(exc)
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
    effective_branch = branch_id or _bid(user)
    try:
        return await statement_service.export_statement(
            restaurant_id=restaurant_id,
            branch_id=effective_branch,
            from_date=from_date,
            to_date=to_date,
            settlement_status=settlement_status,
        )
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
    except Exception as exc:
        result["db_error"] = str(exc)
    return result


@router.get(
    "/fee-calculator",
    summary="Preview Bittu fee for a given gross amount",
    description=(
        "Returns real-time fee breakdown for a hypothetical gross amount:\n"
        "  gross × 0.15% = Bittu fee\n"
        "  fee × 18% = GST on fee\n"
        "  net = gross − fee − GST\n\n"
        "Useful for the frontend to show merchants what they will receive."
    ),
)
async def fee_calculator(
    amount: float = Query(..., gt=0, description="Gross amount to calculate fee for"),
    user: UserContext = Depends(require_permission("statements.read")),
):
    return statement_service.calculate_fee(amount)


# ══════════════════════════════════════════════════════════════════════════════
# 9. ADMIN: ADVANCE SETTLEMENT STATUS
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/settlements/{settlement_id}/advance",
    summary="[Admin] Advance settlement to next status",
    description=(
        "Advances a settlement through its lifecycle:\n"
        "  pending → processing → sent_to_bank → settled | failed\n\n"
        "On 'settled': creates accounting journal entry + updates daily closing.\n"
        "Requires `statements.admin` permission."
    ),
)
async def advance_settlement(
    settlement_id: str,
    body: AdvanceSettlementRequest,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
    user: UserContext = Depends(require_permission("statements.admin")),
):
    restaurant_id = _rid(user)
    actor_id = user.owner_id if user.is_branch_user else user.user_id
    try:
        return await statement_service.transition_settlement(
            settlement_id=settlement_id,
            restaurant_id=restaurant_id,
            new_status=body.new_status,
            actor_id=actor_id,
            actor_type="user",
            bank_reference=body.bank_reference_number,
            failure_reason=body.failure_reason,
            metadata=body.metadata,
        )
    except NotFoundError as exc:
        raise _handle_not_found(exc)
    except ValidationError as exc:
        raise _handle_validation(exc)
    except Exception as exc:
        logger.error("advance_settlement_failed", settlement_id=settlement_id, error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to advance settlement")


# ══════════════════════════════════════════════════════════════════════════════
# 10. INTERNAL: ENQUEUE PAYMENT FOR SETTLEMENT
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/payments/{payment_id}/enqueue",
    status_code=201,
    summary="[Internal] Enqueue a completed payment for settlement",
    description=(
        "Called internally by the payment service when a payment completes.\n"
        "Creates or appends to today's pending settlement batch for the restaurant.\n"
        "Idempotent: duplicate calls for the same payment_id are safe (returns existing)."
    ),
)
async def enqueue_payment(
    payment_id: str,
    body: EnqueuePaymentRequest,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
    user: UserContext = Depends(require_permission("statements.admin")),
):
    restaurant_id = body.restaurant_id or _rid(user)
    branch_id     = body.branch_id     or _bid(user)
    actor_id      = user.owner_id if user.is_branch_user else user.user_id
    try:
        return await statement_service.enqueue_payment_for_settlement(
            payment_id=payment_id,
            order_id=body.order_id,
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            gross_amount=body.gross_amount,
            payment_method=body.payment_method,
            customer_name=body.customer_name,
            order_reference=body.order_reference,
            cycle=body.cycle,
            actor_id=actor_id,
        )
    except ValidationError as exc:
        raise _handle_validation(exc)
    except Exception as exc:
        logger.error("enqueue_payment_failed", payment_id=payment_id, error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to enqueue payment for settlement")
