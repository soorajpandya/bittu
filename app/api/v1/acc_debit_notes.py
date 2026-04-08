"""Debit Notes (Customer) CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import (
    acc_list, acc_get, acc_create, acc_update, acc_delete,
    acc_status_update, acc_line_items_create, acc_line_items_replace, acc_line_items_get,
)

router = APIRouter(prefix="/accounting/debitnotes", tags=["Accounting – Debit Notes"])

TABLE = "acc_debit_notes"
PK = "debit_note_id"
LABEL = "Debit Note"
LINE_PARENT = "debit_note"


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


class DebitNoteCreate(BaseModel):
    customer_id: UUID
    debit_note_number: Optional[str] = None
    reference_number: Optional[str] = None
    date: Optional[str] = None
    status: str = "draft"
    currency_code: Optional[str] = None
    exchange_rate: float = 1.0
    notes: Optional[str] = None
    terms: Optional[str] = None
    discount: float = 0
    discount_type: str = "entity_level"
    adjustment: float = 0
    adjustment_description: Optional[str] = None
    template_id: Optional[UUID] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    line_items: Optional[list[LineItem]] = None


class DebitNoteUpdate(BaseModel):
    customer_id: Optional[UUID] = None
    debit_note_number: Optional[str] = None
    reference_number: Optional[str] = None
    date: Optional[str] = None
    currency_code: Optional[str] = None
    exchange_rate: Optional[float] = None
    notes: Optional[str] = None
    terms: Optional[str] = None
    discount: Optional[float] = None
    discount_type: Optional[str] = None
    adjustment: Optional[float] = None
    adjustment_description: Optional[str] = None
    template_id: Optional[UUID] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    line_items: Optional[list[LineItem]] = None


def _calc_totals(data: dict, items: list[dict]) -> dict:
    sub = sum(i.get("quantity", 1) * i.get("rate", 0) - i.get("discount", 0) for i in items)
    data["sub_total"] = round(sub, 2)
    data["total"] = round(sub - data.get("discount", 0) + data.get("adjustment", 0), 2)
    return data


@router.get("")
async def list_debit_notes(
    user: UserContext = Depends(_auth),
    customer_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"customer_id": customer_id, "status": status}, page=page, per_page=per_page, search_fields=["debit_note_number", "reference_number"])


@router.post("", status_code=201)
async def create_debit_note(body: DebitNoteCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    items = [li.model_dump() for li in (body.line_items or [])]
    data.pop("line_items", None)
    data = _calc_totals(data, items)
    row = await acc_create(TABLE, data, user)
    if items:
        await acc_line_items_create(row[PK], LINE_PARENT, items, user)
    row["line_items"] = await acc_line_items_get(row[PK], LINE_PARENT, user)
    return row


@router.get("/{debit_note_id}")
async def get_debit_note(debit_note_id: UUID, user: UserContext = Depends(_auth)):
    row = await acc_get(TABLE, PK, debit_note_id, user, LABEL)
    row["line_items"] = await acc_line_items_get(debit_note_id, LINE_PARENT, user)
    return row


@router.put("/{debit_note_id}")
async def update_debit_note(debit_note_id: UUID, body: DebitNoteUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    items = None
    if body.line_items is not None:
        items = [li.model_dump() for li in body.line_items]
        data.pop("line_items", None)
        data = _calc_totals(data, items)
    row = await acc_update(TABLE, PK, debit_note_id, data, user, LABEL)
    if items is not None:
        await acc_line_items_replace(debit_note_id, LINE_PARENT, items, user)
    row["line_items"] = await acc_line_items_get(debit_note_id, LINE_PARENT, user)
    return row


@router.delete("/{debit_note_id}")
async def delete_debit_note(debit_note_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, debit_note_id, user, LABEL)


@router.post("/{debit_note_id}/status/sent")
async def mark_sent(debit_note_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, debit_note_id, "sent", user, LABEL)


@router.post("/{debit_note_id}/status/void")
async def mark_void(debit_note_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, debit_note_id, "void", user, LABEL)


# ---------------------------------------------------------------------------
# PDF & Print endpoints
# ---------------------------------------------------------------------------

@router.get("/{debitnote_id}/pdf")
async def get_debitnote_pdf(debitnote_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_document_pdf
    pdf_bytes, filename = await generate_document_pdf("debitnote", debitnote_id, user)
    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{filename}"'})


@router.get("/pdf")
async def bulk_export_debitnote_pdf(
    debitnote_ids: Optional[str] = Query(None, description="Comma-separated IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_bulk_pdf
    ids = [UUID(i.strip()) for i in debitnote_ids.split(",") if i.strip()] if debitnote_ids else []
    if not ids:
        return {"message": "No IDs provided"}
    zip_bytes, filename = await generate_bulk_pdf("debitnote", ids, user)
    return Response(content=zip_bytes, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{debitnote_id}/print")
async def get_debitnote_print(debitnote_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_print_html
    html = await generate_print_html("debitnote", debitnote_id, user)
    return HTMLResponse(content=html)


@router.get("/print")
async def bulk_print_debitnotes(
    debitnote_ids: Optional[str] = Query(None, description="Comma-separated IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_print_html
    ids = [UUID(i.strip()) for i in debitnote_ids.split(",") if i.strip()] if debitnote_ids else []
    if not ids:
        return {"message": "No IDs provided"}
    pages = []
    for doc_id in ids:
        try:
            pages.append(await generate_print_html("debitnote", doc_id, user))
        except ValueError:
            continue
    combined = '<div style="page-break-after:always;"></div>'.join(pages)
    return HTMLResponse(content=combined)
