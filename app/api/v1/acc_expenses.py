"""Expenses CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete, acc_status_update, acc_comments_list, acc_attachment_get, acc_attachment_add, acc_attachment_delete

router = APIRouter(prefix="/accounting/expenses", tags=["Accounting – Expenses"])

TABLE = "acc_expenses"
PK = "expense_id"
LABEL = "Expense"


_auth = require_permission("accounting:read")


class ExpenseCreate(BaseModel):
    account_id: UUID
    date: Optional[str] = None
    amount: float
    paid_through_account_id: Optional[UUID] = None
    vendor_id: Optional[UUID] = None
    customer_id: Optional[UUID] = None
    currency_code: Optional[str] = None
    exchange_rate: float = 1.0
    tax_id: Optional[UUID] = None
    is_inclusive_tax: bool = False
    reference_number: Optional[str] = None
    description: Optional[str] = None
    is_billable: bool = False
    project_id: Optional[UUID] = None
    mileage_type: Optional[str] = None
    mileage_rate: float = 0
    distance: float = 0
    start_reading: Optional[str] = None
    end_reading: Optional[str] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


class ExpenseUpdate(BaseModel):
    account_id: Optional[UUID] = None
    date: Optional[str] = None
    amount: Optional[float] = None
    paid_through_account_id: Optional[UUID] = None
    vendor_id: Optional[UUID] = None
    customer_id: Optional[UUID] = None
    currency_code: Optional[str] = None
    exchange_rate: Optional[float] = None
    tax_id: Optional[UUID] = None
    is_inclusive_tax: Optional[bool] = None
    reference_number: Optional[str] = None
    description: Optional[str] = None
    is_billable: Optional[bool] = None
    project_id: Optional[UUID] = None
    mileage_type: Optional[str] = None
    mileage_rate: Optional[float] = None
    distance: Optional[float] = None
    start_reading: Optional[str] = None
    end_reading: Optional[str] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


@router.get("")
async def list_expenses(
    user: UserContext = Depends(_auth),
    vendor_id: Optional[UUID] = Query(None),
    customer_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"vendor_id": vendor_id, "customer_id": customer_id, "status": status}, page=page, per_page=per_page, search_fields=["description", "reference_number"])


@router.post("", status_code=201)
async def create_expense(body: ExpenseCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{expense_id}")
async def get_expense(expense_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, expense_id, user, LABEL)


@router.put("/{expense_id}")
async def update_expense(expense_id: UUID, body: ExpenseUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, expense_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{expense_id}")
async def delete_expense(expense_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, expense_id, user, LABEL)


# ── Update via custom field ─────────────────────────────────────

class ExpenseCustomFieldUpdate(BaseModel):
    field: str
    value: Optional[str] = None


@router.put("")
async def update_expense_custom_field(body: ExpenseCustomFieldUpdate, user: UserContext = Depends(_auth)):
    """Update an expense using a custom field identifier."""
    return await acc_update(TABLE, body.field, body.value, {}, user, LABEL)


# ── Comments ────────────────────────────────────────────────────


class CommentInput(BaseModel):
    description: str


@router.get("/{expense_id}/comments")
async def list_expense_comments(expense_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comments_list(TABLE, PK, expense_id, user)


# ── Employees ───────────────────────────────────────────────────

EMP_TABLE = "acc_employees"
EMP_PK = "employee_id"
EMP_LABEL = "Employee"


class EmployeeCreate(BaseModel):
    name: str
    email: Optional[str] = None
    status: str = "active"


@router.get("/employees")
async def list_employees(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(EMP_TABLE, user, page=page, per_page=per_page, search_fields=["name", "email"])


@router.post("/employees", status_code=201)
async def create_employee(body: EmployeeCreate, user: UserContext = Depends(_auth)):
    return await acc_create(EMP_TABLE, body.model_dump(exclude_none=True), user)


@router.get("/employees/{employee_id}")
async def get_employee(employee_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(EMP_TABLE, EMP_PK, employee_id, user, EMP_LABEL)


@router.delete("/employees/{employee_id}")
async def delete_employee(employee_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(EMP_TABLE, EMP_PK, employee_id, user, EMP_LABEL)


# ── Receipt (documents/receipt JSONB) ───────────────────────────


class AttachmentInput(BaseModel):
    file_name: str
    file_type: str
    file_size_formatted: str


@router.get("/{expense_id}/receipt")
async def get_expense_receipt(expense_id: UUID, user: UserContext = Depends(_auth)):
    rec = await acc_get(TABLE, PK, expense_id, user, LABEL)
    return rec.get("receipt") or rec.get("documents") or []


@router.post("/{expense_id}/receipt", status_code=201)
async def upload_expense_receipt(expense_id: UUID, body: AttachmentInput, user: UserContext = Depends(_auth)):
    return await acc_attachment_add(TABLE, PK, expense_id, body.model_dump(), user, LABEL)


@router.delete("/{expense_id}/receipt")
async def delete_expense_receipt(expense_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_delete(TABLE, PK, expense_id, "", user, LABEL)


# ── Attachment ──────────────────────────────────────────────────


@router.post("/{expense_id}/attachment", status_code=201)
async def add_expense_attachment(expense_id: UUID, body: AttachmentInput, user: UserContext = Depends(_auth)):
    return await acc_attachment_add(TABLE, PK, expense_id, body.model_dump(), user, LABEL)
