"""Accounting endpoints — cash flow, revenue/expense tracking."""
from datetime import date, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, require_role
from app.core.logging import get_logger
from app.services.accounting_service import AccountingService

router = APIRouter(prefix="/accounting", tags=["Accounting"])
_svc = AccountingService()
logger = get_logger(__name__)


class ExpenseCreate(BaseModel):
    amount: float
    category: str
    description: str = ""
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None


@router.get("/cash-flow")
async def cash_flow(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    """Revenue vs expenses for a period (default: last 30 days)."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    if not start_date:
        start_date = date.today() - timedelta(days=30)
    if not end_date:
        end_date = date.today()
    return await _svc.get_cash_flow(uid, branch_id or user.branch_id, start_date, end_date)


@router.get("/entries")
async def list_entries(
    entry_type: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    branch_id: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    """List accounting entries with filters."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await _svc.get_entries(
        uid, branch_id or user.branch_id, entry_type, start_date, end_date, limit, offset
    )


@router.get("/daily-breakdown")
async def daily_breakdown(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    """Revenue and expenses grouped by day."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    if not start_date:
        start_date = date.today() - timedelta(days=30)
    if not end_date:
        end_date = date.today()
    return await _svc.get_daily_breakdown(uid, branch_id or user.branch_id, start_date, end_date)


@router.get("/payment-methods")
async def payment_method_breakdown(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    """Revenue split by payment method."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    if not start_date:
        start_date = date.today() - timedelta(days=30)
    if not end_date:
        end_date = date.today()
    return await _svc.get_payment_method_breakdown(uid, branch_id or user.branch_id, start_date, end_date)


@router.post("/expenses", status_code=201)
async def record_expense(
    body: ExpenseCreate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    """Manually record an expense."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await _svc.record_expense(
        user_id=uid,
        restaurant_id=user.restaurant_id,
        branch_id=user.branch_id,
        amount=body.amount,
        category=body.category,
        description=body.description,
        reference_type=body.reference_type,
        reference_id=body.reference_id,
    )
