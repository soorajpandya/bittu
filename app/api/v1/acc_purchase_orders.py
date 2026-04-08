"""Purchase Orders CRUD endpoints."""
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

router = APIRouter(prefix="/accounting/purchaseorders", tags=["Accounting – Purchase Orders"])

TABLE = "acc_purchase_orders"
PK = "purchase_order_id"
LABEL = "Purchase Order"
LINE_PARENT = "purchase_order"


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


class PurchaseOrderCreate(BaseModel):
    vendor_id: UUID
    purchaseorder_number: Optional[str] = None
    reference_number: Optional[str] = None
    date: Optional[str] = None
    delivery_date: Optional[str] = None
    status: str = "draft"
    currency_code: Optional[str] = None
    exchange_rate: float = 1.0
    notes: Optional[str] = None
    terms: Optional[str] = None
    discount: float = 0
    discount_type: str = "entity_level"
    adjustment: float = 0
    adjustment_description: Optional[str] = None
    ship_via: Optional[str] = None
    attention: Optional[str] = None
    delivery_address: Optional[dict] = None
    payment_terms: Optional[int] = None
    payment_terms_label: Optional[str] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    line_items: Optional[list[LineItem]] = None


class PurchaseOrderUpdate(BaseModel):
    vendor_id: Optional[UUID] = None
    purchaseorder_number: Optional[str] = None
    reference_number: Optional[str] = None
    date: Optional[str] = None
    delivery_date: Optional[str] = None
    currency_code: Optional[str] = None
    exchange_rate: Optional[float] = None
    notes: Optional[str] = None
    terms: Optional[str] = None
    discount: Optional[float] = None
    discount_type: Optional[str] = None
    adjustment: Optional[float] = None
    adjustment_description: Optional[str] = None
    ship_via: Optional[str] = None
    attention: Optional[str] = None
    delivery_address: Optional[dict] = None
    payment_terms: Optional[int] = None
    payment_terms_label: Optional[str] = None
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
async def list_purchase_orders(
    user: UserContext = Depends(_auth),
    vendor_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"vendor_id": vendor_id, "status": status}, page=page, per_page=per_page, search_fields=["purchaseorder_number", "reference_number"])


@router.post("", status_code=201)
async def create_purchase_order(body: PurchaseOrderCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    items = [li.model_dump() for li in (body.line_items or [])]
    data.pop("line_items", None)
    data = _calc_totals(data, items)
    row = await acc_create(TABLE, data, user)
    if items:
        await acc_line_items_create(row[PK], LINE_PARENT, items, user)
    row["line_items"] = await acc_line_items_get(row[PK], LINE_PARENT, user)
    return row


@router.get("/{purchase_order_id}")
async def get_purchase_order(purchase_order_id: UUID, user: UserContext = Depends(_auth)):
    row = await acc_get(TABLE, PK, purchase_order_id, user, LABEL)
    row["line_items"] = await acc_line_items_get(purchase_order_id, LINE_PARENT, user)
    return row


@router.put("/{purchase_order_id}")
async def update_purchase_order(purchase_order_id: UUID, body: PurchaseOrderUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    items = None
    if body.line_items is not None:
        items = [li.model_dump() for li in body.line_items]
        data.pop("line_items", None)
        data = _calc_totals(data, items)
    row = await acc_update(TABLE, PK, purchase_order_id, data, user, LABEL)
    if items is not None:
        await acc_line_items_replace(purchase_order_id, LINE_PARENT, items, user)
    row["line_items"] = await acc_line_items_get(purchase_order_id, LINE_PARENT, user)
    return row


@router.delete("/{purchase_order_id}")
async def delete_purchase_order(purchase_order_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, purchase_order_id, user, LABEL)


@router.post("/{purchase_order_id}/status/open")
async def mark_open(purchase_order_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, purchase_order_id, "open", user, LABEL)


@router.post("/{purchase_order_id}/status/billed")
async def mark_billed(purchase_order_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, purchase_order_id, "billed", user, LABEL)


@router.post("/{purchase_order_id}/status/cancelled")
async def mark_cancelled(purchase_order_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, purchase_order_id, "cancelled", user, LABEL)


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


class CustomFieldPurchaseOrderUpdate(BaseModel):
    custom_field: str
    value: str
    data: dict


# ---------------------------------------------------------------------------
# 1. Update purchase order via custom field
# ---------------------------------------------------------------------------

@router.put("/purchaseorders")
async def update_purchaseorder_by_custom_field(body: CustomFieldPurchaseOrderUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, None, body.model_dump(exclude_none=True), user, LABEL,
                            custom_field=body.custom_field, custom_value=body.value)


# ---------------------------------------------------------------------------
# 2. Update custom fields only
# ---------------------------------------------------------------------------

@router.put("/purchaseorder/{purchase_order_id}/customfields")
async def update_custom_fields(purchase_order_id: UUID, body: CustomFieldUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, purchase_order_id, {"custom_fields": body.custom_fields}, user, LABEL)


# ---------------------------------------------------------------------------
# 3. Submit purchase order
# ---------------------------------------------------------------------------

@router.post("/{purchase_order_id}/submit")
async def submit_purchaseorder(purchase_order_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, purchase_order_id, "submitted", user, LABEL)


# ---------------------------------------------------------------------------
# 4. Approve purchase order
# ---------------------------------------------------------------------------

@router.post("/{purchase_order_id}/approve")
async def approve_purchaseorder(purchase_order_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, purchase_order_id, "approved", user, LABEL)


# ---------------------------------------------------------------------------
# 5. Get email content
# ---------------------------------------------------------------------------

@router.get("/{purchase_order_id}/email")
async def get_email_content(purchase_order_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_email_get(TABLE, PK, purchase_order_id, user, LABEL)


# ---------------------------------------------------------------------------
# 6. Email purchase order
# ---------------------------------------------------------------------------

@router.post("/{purchase_order_id}/email")
async def send_purchaseorder_email(purchase_order_id: UUID, body: EmailInput, user: UserContext = Depends(_auth)):
    return await acc_email_send(TABLE, PK, purchase_order_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 7. Update billing address
# ---------------------------------------------------------------------------

@router.put("/{purchase_order_id}/address/billing")
async def update_billing_address(purchase_order_id: UUID, body: AddressInput, user: UserContext = Depends(_auth)):
    return await acc_address_update(TABLE, PK, purchase_order_id, "billing", body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 8. List templates
# ---------------------------------------------------------------------------

@router.get("/templates")
async def list_templates(user: UserContext = Depends(_auth)):
    return await acc_templates_list("purchase_order", user)


# ---------------------------------------------------------------------------
# 9. Update template
# ---------------------------------------------------------------------------

@router.put("/{purchase_order_id}/templates/{template_id}")
async def update_template(purchase_order_id: UUID, template_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_template_update(TABLE, PK, purchase_order_id, template_id, user, LABEL)


# ---------------------------------------------------------------------------
# 10. Get attachments
# ---------------------------------------------------------------------------

@router.get("/{purchase_order_id}/attachment")
async def get_attachment(purchase_order_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_get(TABLE, PK, purchase_order_id, user, LABEL)


# ---------------------------------------------------------------------------
# 11. Add attachment
# ---------------------------------------------------------------------------

@router.post("/{purchase_order_id}/attachment", status_code=201)
async def add_attachment(purchase_order_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_add(TABLE, PK, purchase_order_id, user, LABEL)


# ---------------------------------------------------------------------------
# 12. Update attachment preference
# ---------------------------------------------------------------------------

@router.put("/{purchase_order_id}/attachment")
async def update_attachment_pref(purchase_order_id: UUID, body: AttachmentInput, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, purchase_order_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 13. Delete attachment
# ---------------------------------------------------------------------------

@router.delete("/{purchase_order_id}/attachment")
async def delete_attachment(purchase_order_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_delete(TABLE, PK, purchase_order_id, user, LABEL)


# ---------------------------------------------------------------------------
# 14. List comments
# ---------------------------------------------------------------------------

@router.get("/{purchase_order_id}/comments")
async def list_comments(purchase_order_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comments_list(TABLE, PK, purchase_order_id, user, LABEL)


# ---------------------------------------------------------------------------
# 15. Add comment
# ---------------------------------------------------------------------------

@router.post("/{purchase_order_id}/comments", status_code=201)
async def add_comment(purchase_order_id: UUID, body: CommentInput, user: UserContext = Depends(_auth)):
    return await acc_comment_add(TABLE, PK, purchase_order_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 16. Update comment
# ---------------------------------------------------------------------------

@router.put("/{purchase_order_id}/comments/{comment_id}")
async def update_comment(purchase_order_id: UUID, comment_id: UUID, body: CommentInput, user: UserContext = Depends(_auth)):
    return await acc_comment_update(TABLE, PK, purchase_order_id, comment_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 17. Delete comment
# ---------------------------------------------------------------------------

@router.delete("/{purchase_order_id}/comments/{comment_id}")
async def delete_comment(purchase_order_id: UUID, comment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comment_delete(TABLE, PK, purchase_order_id, comment_id, user, LABEL)


# ---------------------------------------------------------------------------
# 18. Reject purchase order
# ---------------------------------------------------------------------------

@router.post("/{purchase_order_id}/reject")
async def reject_purchaseorder(purchase_order_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, purchase_order_id, "rejected", user, LABEL)


# ---------------------------------------------------------------------------
# PDF & Print endpoints
# ---------------------------------------------------------------------------

@router.get("/{purchaseorder_id}/pdf")
async def get_purchaseorder_pdf(purchaseorder_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_document_pdf
    pdf_bytes, filename = await generate_document_pdf("purchaseorder", purchaseorder_id, user)
    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{filename}"'})


@router.get("/pdf")
async def bulk_export_purchaseorder_pdf(
    purchaseorder_ids: Optional[str] = Query(None, description="Comma-separated IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_bulk_pdf
    ids = [UUID(i.strip()) for i in purchaseorder_ids.split(",") if i.strip()] if purchaseorder_ids else []
    if not ids:
        return {"message": "No IDs provided"}
    zip_bytes, filename = await generate_bulk_pdf("purchaseorder", ids, user)
    return Response(content=zip_bytes, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{purchaseorder_id}/print")
async def get_purchaseorder_print(purchaseorder_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_print_html
    html = await generate_print_html("purchaseorder", purchaseorder_id, user)
    return HTMLResponse(content=html)


@router.get("/print")
async def bulk_print_purchaseorders(
    purchaseorder_ids: Optional[str] = Query(None, description="Comma-separated IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_print_html
    ids = [UUID(i.strip()) for i in purchaseorder_ids.split(",") if i.strip()] if purchaseorder_ids else []
    if not ids:
        return {"message": "No IDs provided"}
    pages = []
    for doc_id in ids:
        try:
            pages.append(await generate_print_html("purchaseorder", doc_id, user))
        except ValueError:
            continue
    combined = '<div style="page-break-after:always;"></div>'.join(pages)
    return HTMLResponse(content=combined)
