"""Retainer Invoices CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import (
    acc_list, acc_get, acc_create, acc_update, acc_delete,
    acc_status_update, acc_line_items_create, acc_line_items_replace, acc_line_items_get,
    acc_comments_list, acc_comment_add, acc_comment_update, acc_comment_delete,
    acc_attachment_get, acc_attachment_add, acc_attachment_delete,
    acc_email_get, acc_email_send,
    acc_address_update, acc_templates_list, acc_template_update,
)

router = APIRouter(prefix="/accounting/retainerinvoices", tags=["Accounting – Retainer Invoices"])

TABLE = "acc_retainer_invoices"
PK = "retainerinvoice_id"
LABEL = "Retainer Invoice"
LINE_PARENT = "retainer_invoice"


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


class RetainerInvoiceCreate(BaseModel):
    customer_id: UUID
    retainer_number: Optional[str] = None
    reference_number: Optional[str] = None
    date: Optional[str] = None
    due_date: Optional[str] = None
    status: str = "draft"
    currency_code: Optional[str] = None
    exchange_rate: float = 1.0
    notes: Optional[str] = None
    terms: Optional[str] = None
    discount: float = 0
    discount_type: str = "entity_level"
    payment_terms: Optional[int] = None
    payment_terms_label: Optional[str] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    line_items: Optional[list[LineItem]] = None


class RetainerInvoiceUpdate(BaseModel):
    customer_id: Optional[UUID] = None
    retainer_number: Optional[str] = None
    reference_number: Optional[str] = None
    date: Optional[str] = None
    due_date: Optional[str] = None
    currency_code: Optional[str] = None
    exchange_rate: Optional[float] = None
    notes: Optional[str] = None
    terms: Optional[str] = None
    discount: Optional[float] = None
    discount_type: Optional[str] = None
    payment_terms: Optional[int] = None
    payment_terms_label: Optional[str] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    line_items: Optional[list[LineItem]] = None


def _calc_totals(data: dict, items: list[dict]) -> dict:
    sub = sum(i.get("quantity", 1) * i.get("rate", 0) - i.get("discount", 0) for i in items)
    data["sub_total"] = round(sub, 2)
    data["total"] = round(sub - data.get("discount", 0), 2)
    return data


@router.get("")
async def list_retainers(
    user: UserContext = Depends(_auth),
    customer_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"customer_id": customer_id, "status": status}, page=page, per_page=per_page, search_fields=["retainer_number", "reference_number"])


@router.post("", status_code=201)
async def create_retainer(body: RetainerInvoiceCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    items = [li.model_dump() for li in (body.line_items or [])]
    data.pop("line_items", None)
    data = _calc_totals(data, items)
    row = await acc_create(TABLE, data, user)
    if items:
        await acc_line_items_create(row[PK], LINE_PARENT, items, user)
    row["line_items"] = await acc_line_items_get(row[PK], LINE_PARENT, user)
    return row


@router.get("/{retainerinvoice_id}")
async def get_retainer(retainerinvoice_id: UUID, user: UserContext = Depends(_auth)):
    row = await acc_get(TABLE, PK, retainerinvoice_id, user, LABEL)
    row["line_items"] = await acc_line_items_get(retainerinvoice_id, LINE_PARENT, user)
    return row


@router.put("/{retainerinvoice_id}")
async def update_retainer(retainerinvoice_id: UUID, body: RetainerInvoiceUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    items = None
    if body.line_items is not None:
        items = [li.model_dump() for li in body.line_items]
        data.pop("line_items", None)
        data = _calc_totals(data, items)
    row = await acc_update(TABLE, PK, retainerinvoice_id, data, user, LABEL)
    if items is not None:
        await acc_line_items_replace(retainerinvoice_id, LINE_PARENT, items, user)
    row["line_items"] = await acc_line_items_get(retainerinvoice_id, LINE_PARENT, user)
    return row


@router.delete("/{retainerinvoice_id}")
async def delete_retainer(retainerinvoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, retainerinvoice_id, user, LABEL)


@router.post("/{retainerinvoice_id}/status/sent")
async def mark_sent(retainerinvoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, retainerinvoice_id, "sent", user, LABEL)


@router.post("/{retainerinvoice_id}/status/void")
async def mark_void(retainerinvoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, retainerinvoice_id, "void", user, LABEL)


class EmailInput(BaseModel):
    to_mail_ids: list[str]
    cc_mail_ids: Optional[list[str]] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    send_from_org_email_id: Optional[bool] = None


class AddressInput(BaseModel):
    address: Optional[str] = None
    street2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    country: Optional[str] = None
    fax: Optional[str] = None
    phone: Optional[str] = None


class AttachmentInput(BaseModel):
    can_send_in_mail: Optional[bool] = None


class CommentInput(BaseModel):
    description: str
    show_comment_to_clients: Optional[bool] = False


# ---------------------------------------------------------------------------
# 1. Update template
# ---------------------------------------------------------------------------

@router.put("/{retainerinvoice_id}/templates/{template_id}")
async def update_template(retainerinvoice_id: UUID, template_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_template_update(TABLE, PK, retainerinvoice_id, template_id, user, LABEL)


# ---------------------------------------------------------------------------
# 2. Mark as draft
# ---------------------------------------------------------------------------

@router.post("/{retainerinvoice_id}/status/draft")
async def mark_draft(retainerinvoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, retainerinvoice_id, "draft", user, LABEL)


# ---------------------------------------------------------------------------
# 3. Submit retainer invoice
# ---------------------------------------------------------------------------

@router.post("/{retainerinvoice_id}/submit")
async def submit_retainer(retainerinvoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, retainerinvoice_id, "submitted", user, LABEL)


# ---------------------------------------------------------------------------
# 4. Approve retainer invoice
# ---------------------------------------------------------------------------

@router.post("/{retainerinvoice_id}/approve")
async def approve_retainer(retainerinvoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, retainerinvoice_id, "approved", user, LABEL)


# ---------------------------------------------------------------------------
# 5. Get email content
# ---------------------------------------------------------------------------

@router.get("/{retainerinvoice_id}/email")
async def get_email_content(retainerinvoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_email_get(TABLE, PK, retainerinvoice_id, user, LABEL)


# ---------------------------------------------------------------------------
# 6. Email retainer invoice
# ---------------------------------------------------------------------------

@router.post("/{retainerinvoice_id}/email")
async def send_email(retainerinvoice_id: UUID, body: EmailInput, user: UserContext = Depends(_auth)):
    return await acc_email_send(TABLE, PK, retainerinvoice_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 7. Update billing address
# ---------------------------------------------------------------------------

@router.put("/{retainerinvoice_id}/address/billing")
async def update_billing_address(retainerinvoice_id: UUID, body: AddressInput, user: UserContext = Depends(_auth)):
    return await acc_address_update(TABLE, PK, retainerinvoice_id, "billing", body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 8. List retainer invoice templates
# ---------------------------------------------------------------------------

@router.get("/templates")
async def list_templates(user: UserContext = Depends(_auth)):
    return await acc_templates_list("retainer_invoice", user)


# ---------------------------------------------------------------------------
# 9. Get attachments
# ---------------------------------------------------------------------------

@router.get("/{retainerinvoice_id}/attachment")
async def get_attachment(retainerinvoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_get(TABLE, PK, retainerinvoice_id, user, LABEL)


# ---------------------------------------------------------------------------
# 10. Add attachment
# ---------------------------------------------------------------------------

@router.post("/{retainerinvoice_id}/attachment", status_code=201)
async def add_attachment(retainerinvoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_add(TABLE, PK, retainerinvoice_id, user, LABEL)


# ---------------------------------------------------------------------------
# 11. Delete attachment
# ---------------------------------------------------------------------------

@router.delete("/{retainerinvoice_id}/documents/{document_id}")
async def delete_document(retainerinvoice_id: UUID, document_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete("acc_documents", "document_id", document_id, user, "Document")


# ---------------------------------------------------------------------------
# 12. List comments
# ---------------------------------------------------------------------------

@router.get("/{retainerinvoice_id}/comments")
async def list_comments(retainerinvoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comments_list(TABLE, PK, retainerinvoice_id, user, LABEL)


# ---------------------------------------------------------------------------
# 13. Add comment
# ---------------------------------------------------------------------------

@router.post("/{retainerinvoice_id}/comments", status_code=201)
async def add_comment(retainerinvoice_id: UUID, body: CommentInput, user: UserContext = Depends(_auth)):
    return await acc_comment_add(TABLE, PK, retainerinvoice_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 14. Update comment
# ---------------------------------------------------------------------------

@router.put("/{retainerinvoice_id}/comments/{comment_id}")
async def update_comment(retainerinvoice_id: UUID, comment_id: UUID, body: CommentInput, user: UserContext = Depends(_auth)):
    return await acc_comment_update(TABLE, PK, retainerinvoice_id, comment_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 15. Delete comment
# ---------------------------------------------------------------------------

@router.delete("/{retainerinvoice_id}/comments/{comment_id}")
async def delete_comment(retainerinvoice_id: UUID, comment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comment_delete(TABLE, PK, retainerinvoice_id, comment_id, user, LABEL)


# ---------------------------------------------------------------------------
# PDF & Print endpoints
# ---------------------------------------------------------------------------

@router.get("/{retainerinvoice_id}/pdf")
async def get_retainerinvoice_pdf(retainerinvoice_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_document_pdf
    pdf_bytes, filename = await generate_document_pdf("retainerinvoice", retainerinvoice_id, user)
    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{filename}"'})


@router.get("/pdf")
async def bulk_export_retainerinvoice_pdf(
    retainerinvoice_ids: Optional[str] = Query(None, description="Comma-separated IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_bulk_pdf
    ids = [UUID(i.strip()) for i in retainerinvoice_ids.split(",") if i.strip()] if retainerinvoice_ids else []
    if not ids:
        return {"message": "No IDs provided"}
    zip_bytes, filename = await generate_bulk_pdf("retainerinvoice", ids, user)
    return Response(content=zip_bytes, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{retainerinvoice_id}/print")
async def get_retainerinvoice_print(retainerinvoice_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_print_html
    html = await generate_print_html("retainerinvoice", retainerinvoice_id, user)
    return HTMLResponse(content=html)


@router.get("/print")
async def bulk_print_retainerinvoices(
    retainerinvoice_ids: Optional[str] = Query(None, description="Comma-separated IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_print_html
    ids = [UUID(i.strip()) for i in retainerinvoice_ids.split(",") if i.strip()] if retainerinvoice_ids else []
    if not ids:
        return {"message": "No IDs provided"}
    pages = []
    for doc_id in ids:
        try:
            pages.append(await generate_print_html("retainerinvoice", doc_id, user))
        except ValueError:
            continue
    combined = '<div style="page-break-after:always;"></div>'.join(pages)
    return HTMLResponse(content=combined)
