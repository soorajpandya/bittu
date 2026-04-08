"""Recurring Invoices CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import (
    acc_list, acc_get, acc_create, acc_update, acc_delete,
    acc_status_update, acc_line_items_create, acc_line_items_replace, acc_line_items_get,
)

router = APIRouter(prefix="/accounting/recurringinvoices", tags=["Accounting – Recurring Invoices"])

TABLE = "acc_recurring_invoices"
PK = "recurring_invoice_id"
LABEL = "Recurring Invoice"
LINE_PARENT = "recurring_invoice"


_auth = require_permission("accounting:read")


class LineItem(BaseModel):
    item_id: Optional[UUID] = None
    name: str
    description: Optional[str] = None
    account_id: Optional[UUID] = None
    quantity: float = 1
    rate: float = 0
    discount: float = 0
    tax_id: Optional[UUID] = None


class RecurringInvoiceCreate(BaseModel):
    customer_id: UUID
    recurrence_name: Optional[str] = None
    recurrence_frequency: str = "monthly"
    repeat_every: int = 1
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: str = "active"
    currency_code: Optional[str] = None
    exchange_rate: float = 1.0
    discount: float = 0
    discount_type: str = "entity_level"
    notes: Optional[str] = None
    terms: Optional[str] = None
    payment_terms: Optional[int] = None
    payment_terms_label: Optional[str] = None
    salesperson_name: Optional[str] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    line_items: Optional[list[LineItem]] = None


class RecurringInvoiceUpdate(BaseModel):
    customer_id: Optional[UUID] = None
    recurrence_name: Optional[str] = None
    recurrence_frequency: Optional[str] = None
    repeat_every: Optional[int] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    currency_code: Optional[str] = None
    exchange_rate: Optional[float] = None
    discount: Optional[float] = None
    discount_type: Optional[str] = None
    notes: Optional[str] = None
    terms: Optional[str] = None
    payment_terms: Optional[int] = None
    payment_terms_label: Optional[str] = None
    salesperson_name: Optional[str] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    line_items: Optional[list[LineItem]] = None


@router.get("")
async def list_recurring_invoices(
    user: UserContext = Depends(_auth),
    customer_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"customer_id": customer_id, "status": status}, page=page, per_page=per_page, search_fields=["recurrence_name"])


@router.post("", status_code=201)
async def create_recurring_invoice(body: RecurringInvoiceCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    items = [li.model_dump() for li in (body.line_items or [])]
    data.pop("line_items", None)
    row = await acc_create(TABLE, data, user)
    if items:
        await acc_line_items_create(row[PK], LINE_PARENT, items, user)
    row["line_items"] = await acc_line_items_get(row[PK], LINE_PARENT, user)
    return row


@router.get("/{recurring_invoice_id}")
async def get_recurring_invoice(recurring_invoice_id: UUID, user: UserContext = Depends(_auth)):
    row = await acc_get(TABLE, PK, recurring_invoice_id, user, LABEL)
    row["line_items"] = await acc_line_items_get(recurring_invoice_id, LINE_PARENT, user)
    return row


@router.put("/{recurring_invoice_id}")
async def update_recurring_invoice(recurring_invoice_id: UUID, body: RecurringInvoiceUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    items = None
    if body.line_items is not None:
        items = [li.model_dump() for li in body.line_items]
        data.pop("line_items", None)
    row = await acc_update(TABLE, PK, recurring_invoice_id, data, user, LABEL)
    if items is not None:
        await acc_line_items_replace(recurring_invoice_id, LINE_PARENT, items, user)
    row["line_items"] = await acc_line_items_get(recurring_invoice_id, LINE_PARENT, user)
    return row


@router.delete("/{recurring_invoice_id}")
async def delete_recurring_invoice(recurring_invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, recurring_invoice_id, user, LABEL)


@router.post("/{recurring_invoice_id}/status/stop")
async def stop_recurring(recurring_invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, recurring_invoice_id, "stopped", user, LABEL)


@router.post("/{recurring_invoice_id}/status/resume")
async def resume_recurring(recurring_invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, recurring_invoice_id, "active", user, LABEL)
