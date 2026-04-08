"""Credit Notes CRUD endpoints with line items."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import (
    acc_list, acc_get, acc_create, acc_update, acc_delete, acc_status_update,
    acc_line_items_create, acc_line_items_replace, acc_line_items_get,
    acc_comments_list, acc_comment_add, acc_comment_delete,
    acc_email_get, acc_email_send, acc_email_history,
    acc_address_update,
    acc_templates_list, acc_template_update,
    acc_sub_list, acc_sub_create, acc_sub_get, acc_sub_update, acc_sub_delete,
)

router = APIRouter(prefix="/accounting/creditnotes", tags=["Accounting – Credit Notes"])

TABLE = "acc_credit_notes"
PK = "creditnote_id"
LABEL = "Credit Note"


_auth = require_permission("accounting:read")


class LineItemInput(BaseModel):
    item_id: Optional[UUID] = None
    name: Optional[str] = None
    description: Optional[str] = None
    rate: float = 0
    quantity: float = 1
    unit: Optional[str] = None
    tax_id: Optional[UUID] = None
    tax_percentage: float = 0
    product_type: Optional[str] = None
    hsn_or_sac: Optional[str] = None
    account_id: Optional[UUID] = None
    item_order: int = 0


class EmailInput(BaseModel):
    to_mail_ids: list[str]
    cc_mail_ids: Optional[list[str]] = None
    subject: Optional[str] = None
    body: Optional[str] = None


class AddressInput(BaseModel):
    attention: Optional[str] = None
    address: Optional[str] = None
    street2: Optional[str] = None
    state_code: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    country: Optional[str] = None
    phone: Optional[str] = None
    fax: Optional[str] = None


class CreditApplyInput(BaseModel):
    invoice_id: UUID
    amount_applied: float


class RefundCreate(BaseModel):
    date: str
    refund_mode: str
    reference_number: Optional[str] = None
    amount: float
    from_account_id: UUID
    description: Optional[str] = None


class RefundUpdate(BaseModel):
    date: Optional[str] = None
    refund_mode: Optional[str] = None
    reference_number: Optional[str] = None
    amount: Optional[float] = None
    from_account_id: Optional[UUID] = None
    description: Optional[str] = None


class CommentInput(BaseModel):
    description: str


class CreditNoteCreate(BaseModel):
    customer_id: UUID
    creditnote_number: Optional[str] = None
    date: Optional[str] = None
    line_items: list[LineItemInput]
    currency_id: Optional[UUID] = None
    reference_number: Optional[str] = None
    exchange_rate: float = 1.0
    is_inclusive_tax: bool = False
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None
    terms: Optional[str] = None
    gst_treatment: Optional[str] = None
    tax_treatment: Optional[str] = None
    gst_no: Optional[str] = None


class CreditNoteUpdate(BaseModel):
    customer_id: Optional[UUID] = None
    creditnote_number: Optional[str] = None
    date: Optional[str] = None
    line_items: Optional[list[LineItemInput]] = None
    currency_id: Optional[UUID] = None
    reference_number: Optional[str] = None
    exchange_rate: Optional[float] = None
    is_inclusive_tax: Optional[bool] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None
    terms: Optional[str] = None


def _calc(items):
    s = sum(i.rate * i.quantity for i in items)
    t = sum(i.rate * i.quantity * i.tax_percentage / 100 for i in items)
    return {"sub_total": round(s, 2), "tax_total": round(t, 2), "total": round(s + t, 2), "balance": round(s + t, 2)}


@router.get("")
async def list_credit_notes(user: UserContext = Depends(_auth), status: Optional[str] = Query(None), customer_id: Optional[UUID] = Query(None), page: int = Query(1, ge=1), per_page: int = Query(25, ge=1, le=200)):
    return await acc_list(TABLE, user, filters={"status": status, "customer_id": customer_id}, page=page, per_page=per_page, order_by="date DESC")


@router.post("", status_code=201)
async def create_credit_note(body: CreditNoteCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude={"line_items"}, exclude_none=True)
    data.update(_calc(body.line_items))
    cn = await acc_create(TABLE, data, user)
    cn["line_items"] = await acc_line_items_create(cn[PK], "credit_note", [li.model_dump(exclude_none=True) for li in body.line_items], user)
    return cn


@router.get("/{creditnote_id}")
async def get_credit_note(creditnote_id: UUID, user: UserContext = Depends(_auth)):
    cn = await acc_get(TABLE, PK, creditnote_id, user, LABEL)
    cn["line_items"] = await acc_line_items_get(creditnote_id, "credit_note", user)
    return cn


@router.put("/{creditnote_id}")
async def update_credit_note(creditnote_id: UUID, body: CreditNoteUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude={"line_items"}, exclude_unset=True, exclude_none=True)
    if body.line_items is not None:
        data.update(_calc(body.line_items))
    cn = await acc_update(TABLE, PK, creditnote_id, data, user, LABEL)
    if body.line_items is not None:
        cn["line_items"] = await acc_line_items_replace(creditnote_id, "credit_note", [li.model_dump(exclude_none=True) for li in body.line_items], user)
    return cn


@router.delete("/{creditnote_id}")
async def delete_credit_note(creditnote_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, creditnote_id, user, LABEL)


@router.post("/{creditnote_id}/status/sent")
async def mark_sent(creditnote_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, creditnote_id, "sent", user, LABEL)


@router.post("/{creditnote_id}/status/void")
async def mark_void(creditnote_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, creditnote_id, "void", user, LABEL)


# ── Update via custom field ─────────────────────────────────────────────
@router.put("")
async def update_credit_note_by_custom_field(
    custom_field_name: str = Query(...),
    custom_field_value: str = Query(...),
    body: CreditNoteUpdate = ...,
    user: UserContext = Depends(_auth),
):
    return await acc_update(TABLE, PK, None, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL, custom_field_name=custom_field_name, custom_field_value=custom_field_value)


# ── Email ────────────────────────────────────────────────────────────────
@router.get("/{creditnote_id}/email")
async def get_email_content(creditnote_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_email_get(TABLE, PK, creditnote_id, user, LABEL)


@router.post("/{creditnote_id}/email")
async def email_credit_note(creditnote_id: UUID, body: EmailInput, user: UserContext = Depends(_auth)):
    return await acc_email_send(TABLE, PK, creditnote_id, body.model_dump(exclude_none=True), user, LABEL)


# ── Status transitions ──────────────────────────────────────────────────
@router.post("/{creditnote_id}/status/draft")
async def mark_draft(creditnote_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, creditnote_id, "draft", user, LABEL)


@router.post("/{creditnote_id}/status/open")
async def mark_open(creditnote_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, creditnote_id, "open", user, LABEL)


@router.post("/{creditnote_id}/submit")
async def submit_credit_note(creditnote_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, creditnote_id, "submitted", user, LABEL)


@router.post("/{creditnote_id}/approve")
async def approve_credit_note(creditnote_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, creditnote_id, "approved", user, LABEL)


# ── Email history ────────────────────────────────────────────────────────
@router.get("/{creditnote_id}/emailhistory")
async def get_email_history(creditnote_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_email_history(TABLE, PK, creditnote_id, user, LABEL)


# ── Address ──────────────────────────────────────────────────────────────
@router.put("/{creditnote_id}/address/billing")
async def update_billing_address(creditnote_id: UUID, body: AddressInput, user: UserContext = Depends(_auth)):
    return await acc_address_update(TABLE, PK, creditnote_id, "billing", body.model_dump(exclude_none=True), user, LABEL)


@router.put("/{creditnote_id}/address/shipping")
async def update_shipping_address(creditnote_id: UUID, body: AddressInput, user: UserContext = Depends(_auth)):
    return await acc_address_update(TABLE, PK, creditnote_id, "shipping", body.model_dump(exclude_none=True), user, LABEL)


# ── Templates ────────────────────────────────────────────────────────────
@router.get("/templates")
async def list_templates(user: UserContext = Depends(_auth)):
    return await acc_templates_list(TABLE, user)


@router.put("/{creditnote_id}/templates/{template_id}")
async def update_template(creditnote_id: UUID, template_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_template_update(TABLE, PK, creditnote_id, template_id, user, LABEL)


# ── Credit-note invoices (sub-resource) ──────────────────────────────────
INV_SUB = "acc_creditnote_invoices"
INV_PK = "creditnote_invoice_id"


@router.get("/{creditnote_id}/invoices")
async def list_invoices(creditnote_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_list(INV_SUB, PK, creditnote_id, user)


@router.post("/{creditnote_id}/invoices", status_code=201)
async def apply_to_invoice(creditnote_id: UUID, body: CreditApplyInput, user: UserContext = Depends(_auth)):
    data = body.model_dump()
    data["creditnote_id"] = str(creditnote_id)
    return await acc_sub_create(INV_SUB, data, user)


@router.delete("/{creditnote_id}/invoices/{creditnote_invoice_id}")
async def delete_invoice_application(creditnote_id: UUID, creditnote_invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_delete(INV_SUB, INV_PK, creditnote_invoice_id, user, "Credit Note Invoice")


# ── Comments ─────────────────────────────────────────────────────────────
@router.get("/{creditnote_id}/comments")
async def list_comments(creditnote_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comments_list(TABLE, PK, creditnote_id, user)


@router.post("/{creditnote_id}/comments", status_code=201)
async def add_comment(creditnote_id: UUID, body: CommentInput, user: UserContext = Depends(_auth)):
    return await acc_comment_add(TABLE, PK, creditnote_id, body.description, user)


@router.delete("/{creditnote_id}/comments/{comment_id}")
async def delete_comment(creditnote_id: UUID, comment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comment_delete(TABLE, comment_id, user)


# ── Refunds (sub-resource) ───────────────────────────────────────────────
REFUND_SUB = "acc_creditnote_refunds"
REFUND_PK = "creditnote_refund_id"


@router.get("/refunds")
async def list_all_refunds(user: UserContext = Depends(_auth), page: int = Query(1, ge=1), per_page: int = Query(25, ge=1, le=200)):
    return await acc_list(REFUND_SUB, user, page=page, per_page=per_page)


@router.get("/{creditnote_id}/refunds")
async def list_refunds(creditnote_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_list(REFUND_SUB, PK, creditnote_id, user)


@router.post("/{creditnote_id}/refunds", status_code=201)
async def create_refund(creditnote_id: UUID, body: RefundCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    data["creditnote_id"] = str(creditnote_id)
    return await acc_sub_create(REFUND_SUB, data, user)


@router.get("/{creditnote_id}/refunds/{creditnote_refund_id}")
async def get_refund(creditnote_id: UUID, creditnote_refund_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_get(REFUND_SUB, REFUND_PK, creditnote_refund_id, user, "Credit Note Refund")


@router.put("/{creditnote_id}/refunds/{creditnote_refund_id}")
async def update_refund(creditnote_id: UUID, creditnote_refund_id: UUID, body: RefundUpdate, user: UserContext = Depends(_auth)):
    return await acc_sub_update(REFUND_SUB, REFUND_PK, creditnote_refund_id, body.model_dump(exclude_unset=True, exclude_none=True), user, "Credit Note Refund")


@router.delete("/{creditnote_id}/refunds/{creditnote_refund_id}")
async def delete_refund(creditnote_id: UUID, creditnote_refund_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_delete(REFUND_SUB, REFUND_PK, creditnote_refund_id, user, "Credit Note Refund")


# ---------------------------------------------------------------------------
# PDF & Print endpoints
# ---------------------------------------------------------------------------

@router.get("/{creditnote_id}/pdf")
async def get_creditnote_pdf(creditnote_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_document_pdf
    pdf_bytes, filename = await generate_document_pdf("creditnote", creditnote_id, user)
    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{filename}"'})


@router.get("/pdf")
async def bulk_export_creditnote_pdf(
    creditnote_ids: Optional[str] = Query(None, description="Comma-separated IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_bulk_pdf
    ids = [UUID(i.strip()) for i in creditnote_ids.split(",") if i.strip()] if creditnote_ids else []
    if not ids:
        return {"message": "No IDs provided"}
    zip_bytes, filename = await generate_bulk_pdf("creditnote", ids, user)
    return Response(content=zip_bytes, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{creditnote_id}/print")
async def get_creditnote_print(creditnote_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_print_html
    html = await generate_print_html("creditnote", creditnote_id, user)
    return HTMLResponse(content=html)


@router.get("/print")
async def bulk_print_creditnotes(
    creditnote_ids: Optional[str] = Query(None, description="Comma-separated IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_print_html
    ids = [UUID(i.strip()) for i in creditnote_ids.split(",") if i.strip()] if creditnote_ids else []
    if not ids:
        return {"message": "No IDs provided"}
    pages = []
    for doc_id in ids:
        try:
            pages.append(await generate_print_html("creditnote", doc_id, user))
        except ValueError:
            continue
    combined = '<div style="page-break-after:always;"></div>'.join(pages)
    return HTMLResponse(content=combined)
