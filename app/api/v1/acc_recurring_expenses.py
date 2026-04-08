"""Recurring Expenses CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete, acc_status_update

router = APIRouter(prefix="/accounting/recurringexpenses", tags=["Accounting – Recurring Expenses"])

TABLE = "acc_recurring_expenses"
PK = "recurring_expense_id"
LABEL = "Recurring Expense"


_auth = require_permission("accounting:read")


class RecurringExpenseCreate(BaseModel):
    account_id: UUID
    recurrence_name: Optional[str] = None
    recurrence_frequency: str = "monthly"
    repeat_every: int = 1
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    amount: float = 0
    paid_through_account_id: Optional[UUID] = None
    vendor_id: Optional[UUID] = None
    customer_id: Optional[UUID] = None
    currency_code: Optional[str] = None
    exchange_rate: float = 1.0
    tax_id: Optional[UUID] = None
    is_inclusive_tax: bool = False
    description: Optional[str] = None
    is_billable: bool = False
    project_id: Optional[UUID] = None
    status: str = "active"
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


class RecurringExpenseUpdate(BaseModel):
    account_id: Optional[UUID] = None
    recurrence_name: Optional[str] = None
    recurrence_frequency: Optional[str] = None
    repeat_every: Optional[int] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    amount: Optional[float] = None
    paid_through_account_id: Optional[UUID] = None
    vendor_id: Optional[UUID] = None
    customer_id: Optional[UUID] = None
    currency_code: Optional[str] = None
    exchange_rate: Optional[float] = None
    tax_id: Optional[UUID] = None
    is_inclusive_tax: Optional[bool] = None
    description: Optional[str] = None
    is_billable: Optional[bool] = None
    project_id: Optional[UUID] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


@router.get("")
async def list_recurring_expenses(
    user: UserContext = Depends(_auth),
    vendor_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"vendor_id": vendor_id, "status": status}, page=page, per_page=per_page, search_fields=["recurrence_name", "description"])


@router.post("", status_code=201)
async def create_recurring_expense(body: RecurringExpenseCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{recurring_expense_id}")
async def get_recurring_expense(recurring_expense_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, recurring_expense_id, user, LABEL)


@router.put("/{recurring_expense_id}")
async def update_recurring_expense(recurring_expense_id: UUID, body: RecurringExpenseUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, recurring_expense_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{recurring_expense_id}")
async def delete_recurring_expense(recurring_expense_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, recurring_expense_id, user, LABEL)


@router.post("/{recurring_expense_id}/status/stop")
async def stop_recurring(recurring_expense_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, recurring_expense_id, "stopped", user, LABEL)


@router.post("/{recurring_expense_id}/status/resume")
async def resume_recurring(recurring_expense_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, recurring_expense_id, "active", user, LABEL)
