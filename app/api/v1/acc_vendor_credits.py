"""Vendor Credits CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import (
    acc_list, acc_get, acc_create, acc_update, acc_delete,
    acc_status_update, acc_line_items_create, acc_line_items_replace, acc_line_items_get,
    acc_comments_list, acc_comment_add, acc_comment_delete,
    acc_sub_list, acc_sub_create, acc_sub_get, acc_sub_update, acc_sub_delete,
)

router = APIRouter(prefix="/accounting/vendorcredits", tags=["Accounting – Vendor Credits"])

TABLE = "acc_vendor_credits"
PK = "vendor_credit_id"
LABEL = "Vendor Credit"
LINE_PARENT = "vendor_credit"


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


class VendorCreditCreate(BaseModel):
    vendor_id: UUID
    vendor_credit_number: Optional[str] = None
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
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    line_items: Optional[list[LineItem]] = None


class VendorCreditUpdate(BaseModel):
    vendor_id: Optional[UUID] = None
    vendor_credit_number: Optional[str] = None
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
async def list_vendor_credits(
    user: UserContext = Depends(_auth),
    vendor_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"vendor_id": vendor_id, "status": status}, page=page, per_page=per_page, search_fields=["vendor_credit_number", "reference_number"])


@router.post("", status_code=201)
async def create_vendor_credit(body: VendorCreditCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    items = [li.model_dump() for li in (body.line_items or [])]
    data.pop("line_items", None)
    data = _calc_totals(data, items)
    row = await acc_create(TABLE, data, user)
    if items:
        await acc_line_items_create(row[PK], LINE_PARENT, items, user)
    row["line_items"] = await acc_line_items_get(row[PK], LINE_PARENT, user)
    return row


@router.get("/{vendor_credit_id}")
async def get_vendor_credit(vendor_credit_id: UUID, user: UserContext = Depends(_auth)):
    row = await acc_get(TABLE, PK, vendor_credit_id, user, LABEL)
    row["line_items"] = await acc_line_items_get(vendor_credit_id, LINE_PARENT, user)
    return row


@router.put("/{vendor_credit_id}")
async def update_vendor_credit(vendor_credit_id: UUID, body: VendorCreditUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    items = None
    if body.line_items is not None:
        items = [li.model_dump() for li in body.line_items]
        data.pop("line_items", None)
        data = _calc_totals(data, items)
    row = await acc_update(TABLE, PK, vendor_credit_id, data, user, LABEL)
    if items is not None:
        await acc_line_items_replace(vendor_credit_id, LINE_PARENT, items, user)
    row["line_items"] = await acc_line_items_get(vendor_credit_id, LINE_PARENT, user)
    return row


@router.delete("/{vendor_credit_id}")
async def delete_vendor_credit(vendor_credit_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, vendor_credit_id, user, LABEL)


@router.post("/{vendor_credit_id}/status/open")
async def mark_open(vendor_credit_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, vendor_credit_id, "open", user, LABEL)


@router.post("/{vendor_credit_id}/status/void")
async def mark_void(vendor_credit_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, vendor_credit_id, "void", user, LABEL)


# --- Submit / Approve ---

@router.post("/{vendor_credit_id}/submit")
async def submit_vendor_credit(vendor_credit_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, vendor_credit_id, "submitted", user, LABEL)


@router.post("/{vendor_credit_id}/approve")
async def approve_vendor_credit(vendor_credit_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, vendor_credit_id, "approved", user, LABEL)


# --- Bills credited ---

@router.get("/{vendor_credit_id}/bills")
async def list_bills_credited(vendor_credit_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_list("acc_vendor_credit_bills", "vendor_credit_id", vendor_credit_id, user)


class BillApplyInput(BaseModel):
    bill_id: UUID
    amount_applied: float


@router.post("/{vendor_credit_id}/bills", status_code=201)
async def apply_credit_to_bill(vendor_credit_id: UUID, body: BillApplyInput, user: UserContext = Depends(_auth)):
    data = body.model_dump()
    return await acc_sub_create("acc_vendor_credit_bills", "vendor_credit_id", vendor_credit_id, data, user)


@router.delete("/{vendor_credit_id}/bills/{vendor_credit_bill_id}")
async def delete_bill_application(vendor_credit_id: UUID, vendor_credit_bill_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_delete("acc_vendor_credit_bills", "vendor_credit_bill_id", vendor_credit_bill_id, "vendor_credit_id", vendor_credit_id, user)


# --- Refunds ---

class RefundCreate(BaseModel):
    date: str
    refund_mode: str
    reference_number: Optional[str] = None
    amount: float
    account_id: UUID
    description: Optional[str] = None


class RefundUpdate(BaseModel):
    date: Optional[str] = None
    refund_mode: Optional[str] = None
    reference_number: Optional[str] = None
    amount: Optional[float] = None
    account_id: Optional[UUID] = None
    description: Optional[str] = None


@router.get("/refunds")
async def list_all_vendor_credit_refunds(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list("acc_vendor_credit_refunds", user, page=page, per_page=per_page)


@router.get("/{vendor_credit_id}/refunds")
async def list_refunds(vendor_credit_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_list("acc_vendor_credit_refunds", "vendor_credit_id", vendor_credit_id, user)


@router.post("/{vendor_credit_id}/refunds", status_code=201)
async def create_refund(vendor_credit_id: UUID, body: RefundCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    return await acc_sub_create("acc_vendor_credit_refunds", "vendor_credit_id", vendor_credit_id, data, user)


@router.get("/{vendor_credit_id}/refunds/{vendor_credit_refund_id}")
async def get_refund(vendor_credit_id: UUID, vendor_credit_refund_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_get("acc_vendor_credit_refunds", "vendor_credit_refund_id", vendor_credit_refund_id, "vendor_credit_id", vendor_credit_id, user)


@router.put("/{vendor_credit_id}/refunds/{vendor_credit_refund_id}")
async def update_refund(vendor_credit_id: UUID, vendor_credit_refund_id: UUID, body: RefundUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    return await acc_sub_update("acc_vendor_credit_refunds", "vendor_credit_refund_id", vendor_credit_refund_id, "vendor_credit_id", vendor_credit_id, data, user)


@router.delete("/{vendor_credit_id}/refunds/{vendor_credit_refund_id}")
async def delete_refund(vendor_credit_id: UUID, vendor_credit_refund_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_delete("acc_vendor_credit_refunds", "vendor_credit_refund_id", vendor_credit_refund_id, "vendor_credit_id", vendor_credit_id, user)


# --- Comments ---

class CommentInput(BaseModel):
    description: str


@router.get("/{vendor_credit_id}/comments")
async def list_comments(vendor_credit_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comments_list(TABLE, PK, vendor_credit_id, user)


@router.post("/{vendor_credit_id}/comments", status_code=201)
async def add_comment(vendor_credit_id: UUID, body: CommentInput, user: UserContext = Depends(_auth)):
    return await acc_comment_add(TABLE, PK, vendor_credit_id, body.description, user)


@router.delete("/{vendor_credit_id}/comments/{comment_id}")
async def delete_comment(vendor_credit_id: UUID, comment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comment_delete(TABLE, PK, vendor_credit_id, comment_id, user)


# ---------------------------------------------------------------------------
# PDF & Print endpoints
# ---------------------------------------------------------------------------

@router.get("/{vendorcredit_id}/pdf")
async def get_vendorcredit_pdf(vendorcredit_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_document_pdf
    pdf_bytes, filename = await generate_document_pdf("vendorcredit", vendorcredit_id, user)
    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{filename}"'})


@router.get("/pdf")
async def bulk_export_vendorcredit_pdf(
    vendorcredit_ids: Optional[str] = Query(None, description="Comma-separated IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_bulk_pdf
    ids = [UUID(i.strip()) for i in vendorcredit_ids.split(",") if i.strip()] if vendorcredit_ids else []
    if not ids:
        return {"message": "No IDs provided"}
    zip_bytes, filename = await generate_bulk_pdf("vendorcredit", ids, user)
    return Response(content=zip_bytes, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{vendorcredit_id}/print")
async def get_vendorcredit_print(vendorcredit_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_print_html
    html = await generate_print_html("vendorcredit", vendorcredit_id, user)
    return HTMLResponse(content=html)


@router.get("/print")
async def bulk_print_vendorcredits(
    vendorcredit_ids: Optional[str] = Query(None, description="Comma-separated IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_print_html
    ids = [UUID(i.strip()) for i in vendorcredit_ids.split(",") if i.strip()] if vendorcredit_ids else []
    if not ids:
        return {"message": "No IDs provided"}
    pages = []
    for doc_id in ids:
        try:
            pages.append(await generate_print_html("vendorcredit", doc_id, user))
        except ValueError:
            continue
    combined = '<div style="page-break-after:always;"></div>'.join(pages)
    return HTMLResponse(content=combined)
