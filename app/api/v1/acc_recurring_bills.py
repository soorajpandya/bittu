"""Recurring Bills CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import (
    acc_list, acc_get, acc_create, acc_update, acc_delete,
    acc_status_update, acc_line_items_create, acc_line_items_replace, acc_line_items_get,
)

router = APIRouter(prefix="/accounting/recurringbills", tags=["Accounting – Recurring Bills"])

TABLE = "acc_recurring_bills"
PK = "recurring_bill_id"
LABEL = "Recurring Bill"
LINE_PARENT = "recurring_bill"


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


class RecurringBillCreate(BaseModel):
    vendor_id: UUID
    recurrence_name: Optional[str] = None
    recurrence_frequency: str = "monthly"
    repeat_every: int = 1
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: str = "active"
    currency_code: Optional[str] = None
    exchange_rate: float = 1.0
    notes: Optional[str] = None
    terms: Optional[str] = None
    payment_terms: Optional[int] = None
    payment_terms_label: Optional[str] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    line_items: Optional[list[LineItem]] = None


class RecurringBillUpdate(BaseModel):
    vendor_id: Optional[UUID] = None
    recurrence_name: Optional[str] = None
    recurrence_frequency: Optional[str] = None
    repeat_every: Optional[int] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    currency_code: Optional[str] = None
    exchange_rate: Optional[float] = None
    notes: Optional[str] = None
    terms: Optional[str] = None
    payment_terms: Optional[int] = None
    payment_terms_label: Optional[str] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    line_items: Optional[list[LineItem]] = None


@router.get("")
async def list_recurring_bills(
    user: UserContext = Depends(_auth),
    vendor_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"vendor_id": vendor_id, "status": status}, page=page, per_page=per_page, search_fields=["recurrence_name"])


@router.post("", status_code=201)
async def create_recurring_bill(body: RecurringBillCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    items = [li.model_dump() for li in (body.line_items or [])]
    data.pop("line_items", None)
    row = await acc_create(TABLE, data, user)
    if items:
        await acc_line_items_create(row[PK], LINE_PARENT, items, user)
    row["line_items"] = await acc_line_items_get(row[PK], LINE_PARENT, user)
    return row


@router.get("/{recurring_bill_id}")
async def get_recurring_bill(recurring_bill_id: UUID, user: UserContext = Depends(_auth)):
    row = await acc_get(TABLE, PK, recurring_bill_id, user, LABEL)
    row["line_items"] = await acc_line_items_get(recurring_bill_id, LINE_PARENT, user)
    return row


@router.put("/{recurring_bill_id}")
async def update_recurring_bill(recurring_bill_id: UUID, body: RecurringBillUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    items = None
    if body.line_items is not None:
        items = [li.model_dump() for li in body.line_items]
        data.pop("line_items", None)
    row = await acc_update(TABLE, PK, recurring_bill_id, data, user, LABEL)
    if items is not None:
        await acc_line_items_replace(recurring_bill_id, LINE_PARENT, items, user)
    row["line_items"] = await acc_line_items_get(recurring_bill_id, LINE_PARENT, user)
    return row


@router.delete("/{recurring_bill_id}")
async def delete_recurring_bill(recurring_bill_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, recurring_bill_id, user, LABEL)


@router.post("/{recurring_bill_id}/status/stop")
async def stop_recurring(recurring_bill_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, recurring_bill_id, "stopped", user, LABEL)


@router.post("/{recurring_bill_id}/status/resume")
async def resume_recurring(recurring_bill_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, recurring_bill_id, "active", user, LABEL)
