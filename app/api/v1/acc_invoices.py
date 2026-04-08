"""Invoices CRUD endpoints with line items."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import (
    acc_list, acc_get, acc_create, acc_update, acc_delete, acc_status_update,
    acc_line_items_create, acc_line_items_replace, acc_line_items_get,
    acc_comments_list, acc_comment_add, acc_comment_update, acc_comment_delete,
    acc_attachment_get, acc_attachment_add, acc_attachment_delete,
    acc_email_get, acc_email_send, acc_email_history,
    acc_address_update, acc_templates_list, acc_template_update,
    acc_sub_list, acc_sub_create, acc_sub_delete,
)

router = APIRouter(prefix="/accounting/invoices", tags=["Accounting – Invoices"])

TABLE = "acc_invoices"
PK = "invoice_id"
LABEL = "Invoice"


_auth = require_permission("accounting:read")


class LineItemInput(BaseModel):
    item_id: Optional[UUID] = None
    name: Optional[str] = None
    description: Optional[str] = None
    rate: float = 0
    quantity: float = 1
    unit: Optional[str] = None
    discount_amount: float = 0
    discount: float = 0
    tax_id: Optional[UUID] = None
    tax_name: Optional[str] = None
    tax_type: Optional[str] = None
    tax_percentage: float = 0
    product_type: Optional[str] = None
    hsn_or_sac: Optional[str] = None
    account_id: Optional[UUID] = None
    project_id: Optional[UUID] = None
    item_order: int = 0


class InvoiceCreate(BaseModel):
    customer_id: UUID
    line_items: list[LineItemInput]
    invoice_number: Optional[str] = None
    currency_id: Optional[UUID] = None
    reference_number: Optional[str] = None
    template_id: Optional[UUID] = None
    date: Optional[str] = None
    due_date: Optional[str] = None
    payment_terms: Optional[int] = None
    payment_terms_label: Optional[str] = None
    discount: float = 0
    is_discount_before_tax: bool = True
    discount_type: str = "entity_level"
    is_inclusive_tax: bool = False
    exchange_rate: float = 1.0
    location_id: Optional[UUID] = None
    salesperson_name: Optional[str] = None
    salesperson_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None
    terms: Optional[str] = None
    place_of_supply: Optional[str] = None
    vat_treatment: Optional[str] = None
    tax_treatment: Optional[str] = None
    gst_treatment: Optional[str] = None
    gst_no: Optional[str] = None


class InvoiceUpdate(BaseModel):
    customer_id: Optional[UUID] = None
    line_items: Optional[list[LineItemInput]] = None
    invoice_number: Optional[str] = None
    currency_id: Optional[UUID] = None
    reference_number: Optional[str] = None
    template_id: Optional[UUID] = None
    date: Optional[str] = None
    due_date: Optional[str] = None
    payment_terms: Optional[int] = None
    payment_terms_label: Optional[str] = None
    discount: Optional[float] = None
    is_discount_before_tax: Optional[bool] = None
    discount_type: Optional[str] = None
    is_inclusive_tax: Optional[bool] = None
    exchange_rate: Optional[float] = None
    location_id: Optional[UUID] = None
    salesperson_name: Optional[str] = None
    salesperson_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None
    terms: Optional[str] = None
    place_of_supply: Optional[str] = None
    vat_treatment: Optional[str] = None
    tax_treatment: Optional[str] = None
    gst_treatment: Optional[str] = None
    gst_no: Optional[str] = None


def _calc_totals(line_items: list[LineItemInput]) -> dict:
    sub_total = sum((li.rate * li.quantity) - li.discount_amount for li in line_items)
    tax_total = sum((li.rate * li.quantity - li.discount_amount) * li.tax_percentage / 100 for li in line_items)
    return {"sub_total": round(sub_total, 2), "tax_total": round(tax_total, 2), "total": round(sub_total + tax_total, 2), "balance": round(sub_total + tax_total, 2)}


@router.get("")
async def list_invoices(
    user: UserContext = Depends(_auth),
    status: Optional[str] = Query(None),
    customer_id: Optional[UUID] = Query(None),
    date: Optional[str] = Query(None),
    invoice_number: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(
        TABLE, user,
        filters={"status": status, "customer_id": customer_id, "date": date},
        search_fields={"invoice_number": invoice_number},
        page=page, per_page=per_page, order_by="date DESC",
    )


@router.post("", status_code=201)
async def create_invoice(body: InvoiceCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude={"line_items"}, exclude_none=True)
    data.update(_calc_totals(body.line_items))
    invoice = await acc_create(TABLE, data, user)
    items = await acc_line_items_create(
        invoice["invoice_id"], "invoice",
        [li.model_dump(exclude_none=True) for li in body.line_items], user,
    )
    invoice["line_items"] = items
    return invoice


@router.get("/{invoice_id}")
async def get_invoice(invoice_id: UUID, user: UserContext = Depends(_auth)):
    invoice = await acc_get(TABLE, PK, invoice_id, user, LABEL)
    invoice["line_items"] = await acc_line_items_get(invoice_id, "invoice", user)
    return invoice


@router.put("/{invoice_id}")
async def update_invoice(invoice_id: UUID, body: InvoiceUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude={"line_items"}, exclude_unset=True, exclude_none=True)
    if body.line_items is not None:
        data.update(_calc_totals(body.line_items))
    invoice = await acc_update(TABLE, PK, invoice_id, data, user, LABEL)
    if body.line_items is not None:
        items = await acc_line_items_replace(
            invoice_id, "invoice",
            [li.model_dump(exclude_none=True) for li in body.line_items], user,
        )
        invoice["line_items"] = items
    return invoice


@router.delete("/{invoice_id}")
async def delete_invoice(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, invoice_id, user, LABEL)


@router.post("/{invoice_id}/status/sent")
async def mark_sent(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, invoice_id, "sent", user, LABEL)


@router.post("/{invoice_id}/status/void")
async def mark_void(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, invoice_id, "void", user, LABEL)


# ---------------------------------------------------------------------------
# Pydantic models for new endpoints
# ---------------------------------------------------------------------------

class EmailInput(BaseModel):
    to_mail_ids: list[str]
    cc_mail_ids: Optional[list[str]] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    send_from_org_email_id: Optional[bool] = None


class BulkEmailInput(BaseModel):
    invoice_ids: list[UUID]


class AddressInput(BaseModel):
    address: Optional[str] = None
    street2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    country: Optional[str] = None
    fax: Optional[str] = None
    phone: Optional[str] = None


class CustomFieldUpdate(BaseModel):
    custom_fields: list


class CustomFieldInvoiceUpdate(BaseModel):
    custom_field: str
    value: str
    data: dict


class CreditInput(BaseModel):
    creditnote_id: UUID
    amount_applied: float


class CommentInput(BaseModel):
    description: str
    show_comment_to_clients: Optional[bool] = False


class PaymentLinkInput(BaseModel):
    invoice_ids: Optional[list[UUID]] = None


class FromSalesOrderInput(BaseModel):
    salesorder_id: UUID


class MapSalesOrderInput(BaseModel):
    salesorder_id: UUID


class AttachmentPrefInput(BaseModel):
    can_send_in_mail: Optional[bool] = None


# ---------------------------------------------------------------------------
# 1. Update invoice via custom field
# ---------------------------------------------------------------------------

@router.put("/update")
async def update_invoice_by_custom_field(body: CustomFieldInvoiceUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, None, body.model_dump(exclude_none=True), user, LABEL,
                            custom_field=body.custom_field, custom_value=body.value)


# ---------------------------------------------------------------------------
# 2. Update custom fields only
# ---------------------------------------------------------------------------

@router.put("/{invoice_id}/customfields")
async def update_custom_fields(invoice_id: UUID, body: CustomFieldUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, invoice_id, {"custom_fields": body.custom_fields}, user, LABEL)


# ---------------------------------------------------------------------------
# 3. Mark as draft
# ---------------------------------------------------------------------------

@router.post("/{invoice_id}/status/draft")
async def mark_draft(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, invoice_id, "draft", user, LABEL)


# ---------------------------------------------------------------------------
# 4. Submit invoice
# ---------------------------------------------------------------------------

@router.post("/{invoice_id}/submit")
async def submit_invoice(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, invoice_id, "submitted", user, LABEL)


# ---------------------------------------------------------------------------
# 5. Approve invoice
# ---------------------------------------------------------------------------

@router.post("/{invoice_id}/approve")
async def approve_invoice(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, invoice_id, "approved", user, LABEL)


# ---------------------------------------------------------------------------
# 6. Get email content
# ---------------------------------------------------------------------------

@router.get("/{invoice_id}/email")
async def get_email_content(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_email_get(TABLE, PK, invoice_id, user, LABEL)


# ---------------------------------------------------------------------------
# 7. Send invoice email
# ---------------------------------------------------------------------------

@router.post("/{invoice_id}/email")
async def send_invoice_email(invoice_id: UUID, body: EmailInput, user: UserContext = Depends(_auth)):
    return await acc_email_send(TABLE, PK, invoice_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 8. Bulk email invoices
# ---------------------------------------------------------------------------

@router.post("/email")
async def bulk_email_invoices(body: BulkEmailInput, user: UserContext = Depends(_auth)):
    results = []
    for inv_id in body.invoice_ids:
        r = await acc_email_send(TABLE, PK, inv_id, {}, user, LABEL)
        results.append(r)
    return {"message": f"Emails sent for {len(results)} invoice(s)", "results": results}


# ---------------------------------------------------------------------------
# 9. Get email history
# ---------------------------------------------------------------------------

@router.get("/{invoice_id}/emailhistory")
async def get_email_history(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_email_history(TABLE, PK, invoice_id, user, LABEL)


# ---------------------------------------------------------------------------
# 10. Single invoice PDF
# ---------------------------------------------------------------------------

@router.get("/{invoice_id}/pdf")
async def get_invoice_pdf(invoice_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_document_pdf
    pdf_bytes, filename = await generate_document_pdf("invoice", invoice_id, user)
    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{filename}"'})


# ---------------------------------------------------------------------------
# 11. Bulk export as PDF (ZIP)
# ---------------------------------------------------------------------------

@router.get("/pdf")
async def bulk_export_pdf(
    invoice_ids: Optional[str] = Query(None, description="Comma-separated invoice IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_bulk_pdf
    ids = [UUID(i.strip()) for i in invoice_ids.split(",") if i.strip()] if invoice_ids else []
    if not ids:
        return {"message": "No invoice_ids provided"}
    zip_bytes, filename = await generate_bulk_pdf("invoice", ids, user)
    return Response(content=zip_bytes, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------------------
# 12. Single invoice print (HTML)
# ---------------------------------------------------------------------------

@router.get("/{invoice_id}/print")
async def get_invoice_print(invoice_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_print_html
    html = await generate_print_html("invoice", invoice_id, user)
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# 13. Bulk print
# ---------------------------------------------------------------------------

@router.get("/print")
async def bulk_print(
    invoice_ids: Optional[str] = Query(None, description="Comma-separated invoice IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_print_html
    ids = [UUID(i.strip()) for i in invoice_ids.split(",") if i.strip()] if invoice_ids else []
    if not ids:
        return {"message": "No invoice_ids provided"}
    pages = []
    for doc_id in ids:
        try:
            pages.append(await generate_print_html("invoice", doc_id, user))
        except ValueError:
            continue
    combined = '<div style="page-break-after:always;"></div>'.join(pages)
    return HTMLResponse(content=combined)


# ---------------------------------------------------------------------------
# 12. Send payment reminder
# ---------------------------------------------------------------------------

@router.post("/{invoice_id}/paymentreminder")
async def send_payment_reminder(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, invoice_id, "payment_reminder_sent", user, LABEL)


# ---------------------------------------------------------------------------
# 13. Bulk send payment reminders
# ---------------------------------------------------------------------------

@router.post("/paymentreminder")
async def bulk_send_payment_reminders(body: BulkEmailInput, user: UserContext = Depends(_auth)):
    results = []
    for inv_id in body.invoice_ids:
        r = await acc_status_update(TABLE, PK, inv_id, "payment_reminder_sent", user, LABEL)
        results.append(r)
    return {"message": f"Payment reminders sent for {len(results)} invoice(s)", "results": results}


# ---------------------------------------------------------------------------
# 14. Get payment reminder status
# ---------------------------------------------------------------------------

@router.get("/{invoice_id}/paymentreminder")
async def get_payment_reminder(invoice_id: UUID, user: UserContext = Depends(_auth)):
    inv = await acc_get(TABLE, PK, invoice_id, user, LABEL)
    return {"invoice_id": invoice_id, "payment_reminder_enabled": inv.get("payment_reminder_enabled", False)}


# ---------------------------------------------------------------------------
# 15. Enable payment reminder
# ---------------------------------------------------------------------------

@router.post("/{invoice_id}/paymentreminder/enable")
async def enable_payment_reminder(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, invoice_id, {"payment_reminder_enabled": True}, user, LABEL)


# ---------------------------------------------------------------------------
# 16. Disable payment reminder
# ---------------------------------------------------------------------------

@router.post("/{invoice_id}/paymentreminder/disable")
async def disable_payment_reminder(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, invoice_id, {"payment_reminder_enabled": False}, user, LABEL)


# ---------------------------------------------------------------------------
# 17. Write off invoice
# ---------------------------------------------------------------------------

@router.post("/{invoice_id}/writeoff")
async def write_off_invoice(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, invoice_id, "written_off", user, LABEL)


# ---------------------------------------------------------------------------
# 18. Cancel write off
# ---------------------------------------------------------------------------

@router.post("/{invoice_id}/cancelwriteoff")
async def cancel_write_off(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, invoice_id, "cancel_written_off", user, LABEL)


# ---------------------------------------------------------------------------
# 19. Update billing address
# ---------------------------------------------------------------------------

@router.put("/{invoice_id}/address/billing")
async def update_billing_address(invoice_id: UUID, body: AddressInput, user: UserContext = Depends(_auth)):
    return await acc_address_update(TABLE, PK, invoice_id, "billing", body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 20. Update shipping address
# ---------------------------------------------------------------------------

@router.put("/{invoice_id}/address/shipping")
async def update_shipping_address(invoice_id: UUID, body: AddressInput, user: UserContext = Depends(_auth)):
    return await acc_address_update(TABLE, PK, invoice_id, "shipping", body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 21. List invoice templates
# ---------------------------------------------------------------------------

@router.get("/templates")
async def list_templates(user: UserContext = Depends(_auth)):
    return await acc_templates_list("invoice", user)


# ---------------------------------------------------------------------------
# 22. Update template
# ---------------------------------------------------------------------------

@router.put("/{invoice_id}/templates/{template_id}")
async def update_template(invoice_id: UUID, template_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_template_update(TABLE, PK, invoice_id, template_id, user, LABEL)


# ---------------------------------------------------------------------------
# 23. List payments of invoice
# ---------------------------------------------------------------------------

@router.get("/{invoice_id}/payments")
async def list_payments(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_list("acc_invoice_payments", "invoice_id", invoice_id, user)


# ---------------------------------------------------------------------------
# 23b. Apply payment to invoice
# ---------------------------------------------------------------------------

@router.post("/{invoice_id}/payments", status_code=201)
async def apply_payment(invoice_id: UUID, body: dict, user: UserContext = Depends(_auth)):
    body["invoice_id"] = str(invoice_id)
    return await acc_sub_create("acc_invoice_payments", "invoice_id", invoice_id, body, user)


# ---------------------------------------------------------------------------
# 24. List credits applied
# ---------------------------------------------------------------------------

@router.get("/{invoice_id}/credits")
async def list_credits(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_list("acc_invoice_credits", "invoice_id", invoice_id, user)


# ---------------------------------------------------------------------------
# 24b. List credits applied (alias for frontend /creditsapplied)
# ---------------------------------------------------------------------------

@router.get("/{invoice_id}/creditsapplied")
async def list_credits_applied(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_list("acc_invoice_credits", "invoice_id", invoice_id, user)


# ---------------------------------------------------------------------------
# 25. Apply credit to invoice
# ---------------------------------------------------------------------------

@router.post("/{invoice_id}/credits", status_code=201)
async def apply_credit(invoice_id: UUID, body: CreditInput, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    data["invoice_id"] = str(invoice_id)
    return await acc_sub_create("acc_invoice_credits", "invoice_id", invoice_id, data, user)


# ---------------------------------------------------------------------------
# 26. Delete credit application
# ---------------------------------------------------------------------------

@router.delete("/{invoice_id}/credits/{invoice_credit_id}")
async def delete_credit(invoice_id: UUID, invoice_credit_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_delete("acc_invoice_credits", "invoice_credit_id", invoice_credit_id, user)


# ---------------------------------------------------------------------------
# 27. Get attachment
# ---------------------------------------------------------------------------

@router.get("/{invoice_id}/attachment")
async def get_attachment(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_get(TABLE, PK, invoice_id, user, LABEL)


# ---------------------------------------------------------------------------
# 28. Add attachment
# ---------------------------------------------------------------------------

@router.post("/{invoice_id}/attachment", status_code=201)
async def add_attachment(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_add(TABLE, PK, invoice_id, user, LABEL)


# ---------------------------------------------------------------------------
# 29. Update attachment preference
# ---------------------------------------------------------------------------

@router.put("/{invoice_id}/attachment")
async def update_attachment_pref(invoice_id: UUID, body: AttachmentPrefInput, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, invoice_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 30. Delete attachment
# ---------------------------------------------------------------------------

@router.delete("/{invoice_id}/attachment")
async def delete_attachment(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_delete(TABLE, PK, invoice_id, user, LABEL)


# ---------------------------------------------------------------------------
# 31. Get document details
# ---------------------------------------------------------------------------

@router.get("/{invoice_id}/documents/{document_id}")
async def get_document(invoice_id: UUID, document_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get("acc_documents", "document_id", document_id, user, "Document")


# ---------------------------------------------------------------------------
# 32. Delete document
# ---------------------------------------------------------------------------

@router.delete("/{invoice_id}/documents/{document_id}")
async def delete_document(invoice_id: UUID, document_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete("acc_documents", "document_id", document_id, user, "Document")


# ---------------------------------------------------------------------------
# 33. Delete expense receipt
# ---------------------------------------------------------------------------

@router.delete("/expenses/{expense_id}/receipt")
async def delete_expense_receipt(expense_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_delete("acc_expenses", "expense_id", expense_id, user, "Expense receipt")


# ---------------------------------------------------------------------------
# 34. List comments
# ---------------------------------------------------------------------------

@router.get("/{invoice_id}/comments")
async def list_comments(invoice_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comments_list(TABLE, PK, invoice_id, user, LABEL)


# ---------------------------------------------------------------------------
# 35. Add comment
# ---------------------------------------------------------------------------

@router.post("/{invoice_id}/comments", status_code=201)
async def add_comment(invoice_id: UUID, body: CommentInput, user: UserContext = Depends(_auth)):
    return await acc_comment_add(TABLE, PK, invoice_id, body.description, user, LABEL)


# ---------------------------------------------------------------------------
# 36. Update comment
# ---------------------------------------------------------------------------

@router.put("/{invoice_id}/comments/{comment_id}")
async def update_comment(invoice_id: UUID, comment_id: UUID, body: CommentInput, user: UserContext = Depends(_auth)):
    return await acc_comment_update(TABLE, PK, invoice_id, str(comment_id), body.description, user, LABEL)


# ---------------------------------------------------------------------------
# 37. Delete comment
# ---------------------------------------------------------------------------

@router.delete("/{invoice_id}/comments/{comment_id}")
async def delete_comment(invoice_id: UUID, comment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comment_delete(TABLE, PK, invoice_id, str(comment_id), user, LABEL)


# ---------------------------------------------------------------------------
# 38. Generate payment link
# ---------------------------------------------------------------------------

@router.get("/share/paymentlink")
async def get_payment_link(
    invoice_id: UUID = Query(...),
    user: UserContext = Depends(_auth),
):
    inv = await acc_get(TABLE, PK, invoice_id, user, LABEL)
    return {"invoice_id": invoice_id, "payment_link": inv.get("payment_link")}


# ---------------------------------------------------------------------------
# 39. Create invoice from sales order
# ---------------------------------------------------------------------------

@router.post("/fromsalesorder", status_code=201)
async def create_from_sales_order(body: FromSalesOrderInput, user: UserContext = Depends(_auth)):
    so = await acc_get("acc_sales_orders", "salesorder_id", body.salesorder_id, user, "Sales Order")
    data = {"salesorder_id": str(body.salesorder_id), "customer_id": so.get("customer_id"), "status": "draft"}
    return await acc_create(TABLE, data, user)


# ---------------------------------------------------------------------------
# 40. Map invoice to sales order
# ---------------------------------------------------------------------------

@router.post("/{invoice_id}/map/salesorder")
async def map_to_sales_order(invoice_id: UUID, body: MapSalesOrderInput, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, invoice_id, {"salesorder_id": str(body.salesorder_id)}, user, LABEL)
