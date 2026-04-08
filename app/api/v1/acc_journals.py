"""Journals (Manual Journal Entries) CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import (
    acc_list, acc_get, acc_create, acc_update, acc_delete,
    acc_status_update, acc_line_items_create, acc_line_items_replace, acc_line_items_get,
)

router = APIRouter(prefix="/accounting/journals", tags=["Accounting – Journals"])

TABLE = "acc_journals"
PK = "journal_id"
LABEL = "Journal"
LINE_PARENT = "journal"


_auth = require_permission("accounting:read")


class JournalLineItem(BaseModel):
    account_id: UUID
    description: Optional[str] = None
    debit_or_credit: str  # "debit" or "credit"
    amount: float
    contact_id: Optional[UUID] = None
    tax_id: Optional[UUID] = None
    name: str = ""


class JournalCreate(BaseModel):
    journal_date: Optional[str] = None
    reference_number: Optional[str] = None
    notes: Optional[str] = None
    currency_code: Optional[str] = None
    exchange_rate: float = 1.0
    journal_type: Optional[str] = None
    status: str = "draft"
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    line_items: Optional[list[JournalLineItem]] = None


class JournalUpdate(BaseModel):
    journal_date: Optional[str] = None
    reference_number: Optional[str] = None
    notes: Optional[str] = None
    currency_code: Optional[str] = None
    exchange_rate: Optional[float] = None
    journal_type: Optional[str] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    line_items: Optional[list[JournalLineItem]] = None


def _calc_totals(data: dict, items: list[dict]) -> dict:
    total_debit = sum(i["amount"] for i in items if i.get("debit_or_credit") == "debit")
    total_credit = sum(i["amount"] for i in items if i.get("debit_or_credit") == "credit")
    data["total"] = round(total_debit, 2)
    data["sub_total"] = round(total_debit, 2)
    return data


@router.get("")
async def list_journals(
    user: UserContext = Depends(_auth),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"status": status}, page=page, per_page=per_page, search_fields=["reference_number", "notes"], order_by="journal_date DESC")


@router.post("", status_code=201)
async def create_journal(body: JournalCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    items = [li.model_dump() for li in (body.line_items or [])]
    data.pop("line_items", None)
    data = _calc_totals(data, items)
    row = await acc_create(TABLE, data, user)
    if items:
        await acc_line_items_create(row[PK], LINE_PARENT, items, user)
    row["line_items"] = await acc_line_items_get(row[PK], LINE_PARENT, user)
    return row


@router.get("/{journal_id}")
async def get_journal(journal_id: UUID, user: UserContext = Depends(_auth)):
    row = await acc_get(TABLE, PK, journal_id, user, LABEL)
    row["line_items"] = await acc_line_items_get(journal_id, LINE_PARENT, user)
    return row


@router.put("/{journal_id}")
async def update_journal(journal_id: UUID, body: JournalUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    items = None
    if body.line_items is not None:
        items = [li.model_dump() for li in body.line_items]
        data.pop("line_items", None)
        data = _calc_totals(data, items)
    row = await acc_update(TABLE, PK, journal_id, data, user, LABEL)
    if items is not None:
        await acc_line_items_replace(journal_id, LINE_PARENT, items, user)
    row["line_items"] = await acc_line_items_get(journal_id, LINE_PARENT, user)
    return row


@router.delete("/{journal_id}")
async def delete_journal(journal_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, journal_id, user, LABEL)


@router.post("/{journal_id}/status/published")
async def mark_published(journal_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, journal_id, "published", user, LABEL)
