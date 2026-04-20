"""Expense management API endpoints."""
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.core.logging import get_logger
from app.services.expense_service import expense_service

router = APIRouter(prefix="/expenses", tags=["Expenses"])
logger = get_logger(__name__)


class ExpenseCreate(BaseModel):
    category_id: Optional[str] = None
    category_name: Optional[str] = None
    vendor_id: Optional[str] = None
    vendor_name: Optional[str] = None
    amount: float
    tax_amount: float = 0
    payment_method: str = "cash"
    payment_status: str = "paid"
    expense_date: Optional[date] = None
    description: str = ""
    receipt_url: Optional[str] = None
    invoice_number: Optional[str] = None
    is_recurring: bool = False
    recurrence: Optional[str] = None


class CategoryCreate(BaseModel):
    name: str
    account_code: str
    description: str = ""


@router.post("")
async def create_expense(
    body: ExpenseCreate,
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("expense.write")),
):
    """Create a new expense."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await expense_service.create_expense(
        restaurant_id=uid,
        branch_id=branch_id or (user.branch_id if user.is_branch_user else None),
        category_id=body.category_id,
        category_name=body.category_name,
        vendor_id=body.vendor_id,
        vendor_name=body.vendor_name,
        amount=body.amount,
        tax_amount=body.tax_amount,
        payment_method=body.payment_method,
        payment_status=body.payment_status,
        expense_date=body.expense_date,
        description=body.description,
        receipt_url=body.receipt_url,
        invoice_number=body.invoice_number,
        is_recurring=body.is_recurring,
        recurrence=body.recurrence,
        created_by=user.user_id,
    )


@router.get("")
async def list_expenses(
    category_id: Optional[str] = Query(None),
    vendor_id: Optional[str] = Query(None),
    payment_status: Optional[str] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("expense.read")),
):
    """List expenses with filters."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await expense_service.list_expenses(
        restaurant_id=uid,
        category_id=category_id,
        vendor_id=vendor_id,
        payment_status=payment_status,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        offset=offset,
    )


@router.get("/summary")
async def expense_summary(
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("expense.read")),
):
    """Get expense summary by category."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await expense_service.expense_summary(uid, from_date, to_date)


@router.get("/categories")
async def list_categories(
    user: UserContext = Depends(require_permission("expense.read")),
):
    """List expense categories."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await expense_service.list_categories(uid)


@router.post("/categories")
async def create_category(
    body: CategoryCreate,
    user: UserContext = Depends(require_permission("expense.write")),
):
    """Create a new expense category."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await expense_service.create_category(uid, body.name, body.account_code, body.description)


@router.get("/{expense_id}")
async def get_expense(
    expense_id: str,
    user: UserContext = Depends(require_permission("expense.read")),
):
    """Get expense details."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await expense_service.get_expense(expense_id, uid)


@router.post("/{expense_id}/approve")
async def approve_expense(
    expense_id: str,
    user: UserContext = Depends(require_permission("expense.approve")),
):
    """Approve an expense."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await expense_service.approve_expense(expense_id, uid, user.user_id)
