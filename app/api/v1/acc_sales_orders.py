"""Sales Orders CRUD endpoints with line items."""
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
    acc_email_get, acc_email_send,
    acc_address_update, acc_templates_list, acc_template_update,
)

router = APIRouter(prefix="/accounting/salesorders", tags=["Accounting – Sales Orders"])

TABLE = "acc_sales_orders"
PK = "salesorder_id"
LABEL = "Sales Order"


_auth = require_permission("accounting:read")


class LineItemInput(BaseModel):
    item_id: Optional[UUID] = None
    name: Optional[str] = None
    description: Optional[str] = None
    rate: float = 0
    quantity: float = 1
    unit: Optional[str] = None
    discount: float = 0
    tax_id: Optional[UUID] = None
    tax_percentage: float = 0
    product_type: Optional[str] = None
    hsn_or_sac: Optional[str] = None
    item_order: int = 0


class SalesOrderCreate(BaseModel):
    customer_id: UUID
    line_items: list[LineItemInput]
    salesorder_number: Optional[str] = None
    currency_id: Optional[UUID] = None
    reference_number: Optional[str] = None
    date: Optional[str] = None
    shipment_date: Optional[str] = None
    delivery_method: Optional[str] = None
    exchange_rate: float = 1.0
    discount: float = 0
    is_discount_before_tax: bool = True
    discount_type: str = "entity_level"
    is_inclusive_tax: bool = False
    salesperson_id: Optional[UUID] = None
    salesperson_name: Optional[str] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None
    terms: Optional[str] = None
    place_of_supply: Optional[str] = None
    gst_treatment: Optional[str] = None
    gst_no: Optional[str] = None


class SalesOrderUpdate(BaseModel):
    customer_id: Optional[UUID] = None
    line_items: Optional[list[LineItemInput]] = None
    salesorder_number: Optional[str] = None
    currency_id: Optional[UUID] = None
    reference_number: Optional[str] = None
    date: Optional[str] = None
    shipment_date: Optional[str] = None
    delivery_method: Optional[str] = None
    exchange_rate: Optional[float] = None
    discount: Optional[float] = None
    is_discount_before_tax: Optional[bool] = None
    discount_type: Optional[str] = None
    is_inclusive_tax: Optional[bool] = None
    salesperson_id: Optional[UUID] = None
    salesperson_name: Optional[str] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None
    terms: Optional[str] = None


def _calc(items):
    s = sum((i.rate * i.quantity) * (1 - i.discount / 100) for i in items)
    t = sum((i.rate * i.quantity) * (1 - i.discount / 100) * i.tax_percentage / 100 for i in items)
    return {"sub_total": round(s, 2), "tax_total": round(t, 2), "total": round(s + t, 2)}


@router.get("")
async def list_sales_orders(
    user: UserContext = Depends(_auth),
    status: Optional[str] = Query(None),
    customer_id: Optional[UUID] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"status": status, "customer_id": customer_id}, page=page, per_page=per_page, order_by="date DESC")


@router.post("", status_code=201)
async def create_sales_order(body: SalesOrderCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude={"line_items"}, exclude_none=True)
    data.update(_calc(body.line_items))
    so = await acc_create(TABLE, data, user)
    items = await acc_line_items_create(so[PK], "sales_order", [li.model_dump(exclude_none=True) for li in body.line_items], user)
    so["line_items"] = items
    return so


@router.get("/{salesorder_id}")
async def get_sales_order(salesorder_id: UUID, user: UserContext = Depends(_auth)):
    so = await acc_get(TABLE, PK, salesorder_id, user, LABEL)
    so["line_items"] = await acc_line_items_get(salesorder_id, "sales_order", user)
    return so


@router.put("/{salesorder_id}")
async def update_sales_order(salesorder_id: UUID, body: SalesOrderUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude={"line_items"}, exclude_unset=True, exclude_none=True)
    if body.line_items is not None:
        data.update(_calc(body.line_items))
    so = await acc_update(TABLE, PK, salesorder_id, data, user, LABEL)
    if body.line_items is not None:
        so["line_items"] = await acc_line_items_replace(salesorder_id, "sales_order", [li.model_dump(exclude_none=True) for li in body.line_items], user)
    return so


@router.delete("/{salesorder_id}")
async def delete_sales_order(salesorder_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, salesorder_id, user, LABEL)


@router.post("/{salesorder_id}/status/confirmed")
async def confirm(salesorder_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, salesorder_id, "confirmed", user, LABEL)


@router.post("/{salesorder_id}/status/void")
async def void(salesorder_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, salesorder_id, "void", user, LABEL)


# ---------------------------------------------------------------------------
# Pydantic models for new endpoints
# ---------------------------------------------------------------------------

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


class CustomFieldUpdate(BaseModel):
    custom_fields: list


class CustomFieldSalesOrderUpdate(BaseModel):
    custom_field: str
    value: str
    data: dict


# ---------------------------------------------------------------------------
# 1. Update sales order via custom field
# ---------------------------------------------------------------------------

@router.put("/salesorders")
async def update_salesorder_by_custom_field(body: CustomFieldSalesOrderUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, None, body.model_dump(exclude_none=True), user, LABEL,
                            custom_field=body.custom_field, custom_value=body.value)


# ---------------------------------------------------------------------------
# 2. Update custom fields only
# ---------------------------------------------------------------------------

@router.put("/salesorder/{salesorder_id}/customfields")
async def update_custom_fields(salesorder_id: UUID, body: CustomFieldUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, salesorder_id, {"custom_fields": body.custom_fields}, user, LABEL)


# ---------------------------------------------------------------------------
# 3. Mark as open
# ---------------------------------------------------------------------------

@router.post("/{salesorder_id}/status/open")
async def mark_open(salesorder_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, salesorder_id, "open", user, LABEL)


# ---------------------------------------------------------------------------
# 4. Update sub-status
# ---------------------------------------------------------------------------

@router.post("/{salesorder_id}/substatus/{status_code}")
async def update_sub_status(salesorder_id: UUID, status_code: str, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, salesorder_id, status_code, user, LABEL)


# ---------------------------------------------------------------------------
# 5. Get email content
# ---------------------------------------------------------------------------

@router.get("/{salesorder_id}/email")
async def get_email_content(salesorder_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_email_get(TABLE, PK, salesorder_id, user, LABEL)


# ---------------------------------------------------------------------------
# 6. Email sales order
# ---------------------------------------------------------------------------

@router.post("/{salesorder_id}/email")
async def send_salesorder_email(salesorder_id: UUID, body: EmailInput, user: UserContext = Depends(_auth)):
    return await acc_email_send(TABLE, PK, salesorder_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 7. Submit sales order
# ---------------------------------------------------------------------------

@router.post("/{salesorder_id}/submit")
async def submit_salesorder(salesorder_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, salesorder_id, "submitted", user, LABEL)


# ---------------------------------------------------------------------------
# 8. Approve sales order
# ---------------------------------------------------------------------------

@router.post("/{salesorder_id}/approve")
async def approve_salesorder(salesorder_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, salesorder_id, "approved", user, LABEL)


# ---------------------------------------------------------------------------
# 9. Single sales order PDF
# ---------------------------------------------------------------------------

@router.get("/{salesorder_id}/pdf")
async def get_salesorder_pdf(salesorder_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_document_pdf
    pdf_bytes, filename = await generate_document_pdf("salesorder", salesorder_id, user)
    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{filename}"'})


# ---------------------------------------------------------------------------
# 10. Bulk export PDF (ZIP)
# ---------------------------------------------------------------------------

@router.get("/pdf")
async def bulk_export_pdf(
    salesorder_ids: Optional[str] = Query(None, description="Comma-separated sales order IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_bulk_pdf
    ids = [UUID(i.strip()) for i in salesorder_ids.split(",") if i.strip()] if salesorder_ids else []
    if not ids:
        return {"message": "No salesorder_ids provided"}
    zip_bytes, filename = await generate_bulk_pdf("salesorder", ids, user)
    return Response(content=zip_bytes, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------------------
# 11. Single sales order print (HTML)
# ---------------------------------------------------------------------------

@router.get("/{salesorder_id}/print")
async def get_salesorder_print(salesorder_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_print_html
    html = await generate_print_html("salesorder", salesorder_id, user)
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# 12. Bulk print
# ---------------------------------------------------------------------------

@router.get("/print")
async def bulk_print(
    salesorder_ids: Optional[str] = Query(None, description="Comma-separated sales order IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_print_html
    ids = [UUID(i.strip()) for i in salesorder_ids.split(",") if i.strip()] if salesorder_ids else []
    if not ids:
        return {"message": "No salesorder_ids provided"}
    pages = []
    for doc_id in ids:
        try:
            pages.append(await generate_print_html("salesorder", doc_id, user))
        except ValueError:
            continue
    combined = '<div style="page-break-after:always;"></div>'.join(pages)
    return HTMLResponse(content=combined)


# ---------------------------------------------------------------------------
# 11. Update billing address
# ---------------------------------------------------------------------------

@router.put("/{salesorder_id}/address/billing")
async def update_billing_address(salesorder_id: UUID, body: AddressInput, user: UserContext = Depends(_auth)):
    return await acc_address_update(TABLE, PK, salesorder_id, "billing", body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 12. Update shipping address
# ---------------------------------------------------------------------------

@router.put("/{salesorder_id}/address/shipping")
async def update_shipping_address(salesorder_id: UUID, body: AddressInput, user: UserContext = Depends(_auth)):
    return await acc_address_update(TABLE, PK, salesorder_id, "shipping", body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 13. List templates
# ---------------------------------------------------------------------------

@router.get("/templates")
async def list_templates(user: UserContext = Depends(_auth)):
    return await acc_templates_list("sales_order", user)


# ---------------------------------------------------------------------------
# 14. Update template
# ---------------------------------------------------------------------------

@router.put("/{salesorder_id}/templates/{template_id}")
async def update_template(salesorder_id: UUID, template_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_template_update(TABLE, PK, salesorder_id, template_id, user, LABEL)


# ---------------------------------------------------------------------------
# 15. Get attachments
# ---------------------------------------------------------------------------

@router.get("/{salesorder_id}/attachment")
async def get_attachment(salesorder_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_get(TABLE, PK, salesorder_id, user, LABEL)


# ---------------------------------------------------------------------------
# 16. Add attachment
# ---------------------------------------------------------------------------

@router.post("/{salesorder_id}/attachment", status_code=201)
async def add_attachment(salesorder_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_add(TABLE, PK, salesorder_id, user, LABEL)


# ---------------------------------------------------------------------------
# 17. Update attachment preference
# ---------------------------------------------------------------------------

@router.put("/{salesorder_id}/attachment")
async def update_attachment_pref(salesorder_id: UUID, body: AttachmentInput, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, salesorder_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 18. Delete attachment
# ---------------------------------------------------------------------------

@router.delete("/{salesorder_id}/attachment")
async def delete_attachment(salesorder_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_delete(TABLE, PK, salesorder_id, user, LABEL)


# ---------------------------------------------------------------------------
# 19. List comments
# ---------------------------------------------------------------------------

@router.get("/{salesorder_id}/comments")
async def list_comments(salesorder_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comments_list(TABLE, PK, salesorder_id, user, LABEL)


# ---------------------------------------------------------------------------
# 20. Add comment
# ---------------------------------------------------------------------------

@router.post("/{salesorder_id}/comments", status_code=201)
async def add_comment(salesorder_id: UUID, body: CommentInput, user: UserContext = Depends(_auth)):
    return await acc_comment_add(TABLE, PK, salesorder_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 21. Update comment
# ---------------------------------------------------------------------------

@router.put("/{salesorder_id}/comments/{comment_id}")
async def update_comment(salesorder_id: UUID, comment_id: UUID, body: CommentInput, user: UserContext = Depends(_auth)):
    return await acc_comment_update(TABLE, PK, salesorder_id, comment_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 22. Delete comment
# ---------------------------------------------------------------------------

@router.delete("/{salesorder_id}/comments/{comment_id}")
async def delete_comment(salesorder_id: UUID, comment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comment_delete(TABLE, PK, salesorder_id, comment_id, user, LABEL)
