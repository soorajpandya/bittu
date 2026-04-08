"""Estimates CRUD endpoints with line items."""
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
    acc_email_get, acc_email_send,
    acc_address_update, acc_templates_list, acc_template_update,
)

router = APIRouter(prefix="/accounting/estimates", tags=["Accounting – Estimates"])

TABLE = "acc_estimates"
PK = "estimate_id"
LABEL = "Estimate"


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
    item_order: int = 0


class EstimateCreate(BaseModel):
    customer_id: UUID
    line_items: list[LineItemInput]
    estimate_number: Optional[str] = None
    currency_id: Optional[UUID] = None
    reference_number: Optional[str] = None
    template_id: Optional[UUID] = None
    date: Optional[str] = None
    expiry_date: Optional[str] = None
    exchange_rate: float = 1.0
    discount: float = 0
    is_discount_before_tax: bool = True
    discount_type: str = "entity_level"
    is_inclusive_tax: bool = False
    salesperson_name: Optional[str] = None
    custom_body: Optional[str] = None
    custom_subject: Optional[str] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None
    terms: Optional[str] = None
    place_of_supply: Optional[str] = None
    gst_treatment: Optional[str] = None
    gst_no: Optional[str] = None


class EstimateUpdate(BaseModel):
    customer_id: Optional[UUID] = None
    line_items: Optional[list[LineItemInput]] = None
    estimate_number: Optional[str] = None
    currency_id: Optional[UUID] = None
    reference_number: Optional[str] = None
    template_id: Optional[UUID] = None
    date: Optional[str] = None
    expiry_date: Optional[str] = None
    exchange_rate: Optional[float] = None
    discount: Optional[float] = None
    is_discount_before_tax: Optional[bool] = None
    discount_type: Optional[str] = None
    is_inclusive_tax: Optional[bool] = None
    salesperson_name: Optional[str] = None
    custom_body: Optional[str] = None
    custom_subject: Optional[str] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None
    terms: Optional[str] = None


def _calc_totals(line_items):
    sub_total = sum((li.rate * li.quantity) - li.discount_amount for li in line_items)
    tax_total = sum((li.rate * li.quantity - li.discount_amount) * li.tax_percentage / 100 for li in line_items)
    return {"sub_total": round(sub_total, 2), "tax_total": round(tax_total, 2), "total": round(sub_total + tax_total, 2)}


@router.get("")
async def list_estimates(
    user: UserContext = Depends(_auth),
    status: Optional[str] = Query(None),
    customer_id: Optional[UUID] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"status": status, "customer_id": customer_id}, page=page, per_page=per_page, order_by="date DESC")


@router.post("", status_code=201)
async def create_estimate(body: EstimateCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude={"line_items"}, exclude_none=True)
    data.update(_calc_totals(body.line_items))
    estimate = await acc_create(TABLE, data, user)
    items = await acc_line_items_create(estimate["estimate_id"], "estimate", [li.model_dump(exclude_none=True) for li in body.line_items], user)
    estimate["line_items"] = items
    return estimate


@router.get("/{estimate_id}")
async def get_estimate(estimate_id: UUID, user: UserContext = Depends(_auth)):
    estimate = await acc_get(TABLE, PK, estimate_id, user, LABEL)
    estimate["line_items"] = await acc_line_items_get(estimate_id, "estimate", user)
    return estimate


@router.put("/{estimate_id}")
async def update_estimate(estimate_id: UUID, body: EstimateUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude={"line_items"}, exclude_unset=True, exclude_none=True)
    if body.line_items is not None:
        data.update(_calc_totals(body.line_items))
    estimate = await acc_update(TABLE, PK, estimate_id, data, user, LABEL)
    if body.line_items is not None:
        items = await acc_line_items_replace(estimate_id, "estimate", [li.model_dump(exclude_none=True) for li in body.line_items], user)
        estimate["line_items"] = items
    return estimate


@router.delete("/{estimate_id}")
async def delete_estimate(estimate_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, estimate_id, user, LABEL)


@router.post("/{estimate_id}/status/sent")
async def mark_sent(estimate_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, estimate_id, "sent", user, LABEL)


@router.post("/{estimate_id}/status/accepted")
async def mark_accepted(estimate_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, estimate_id, "accepted", user, LABEL)


@router.post("/{estimate_id}/status/declined")
async def mark_declined(estimate_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, estimate_id, "declined", user, LABEL)


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
    estimate_ids: list[UUID]


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


class CustomFieldEstimateUpdate(BaseModel):
    custom_field: str
    value: str
    data: dict


class CommentInput(BaseModel):
    description: str
    show_comment_to_clients: Optional[bool] = False


# ---------------------------------------------------------------------------
# 1. Update estimate via custom field
# ---------------------------------------------------------------------------

@router.put("/estimates")
async def update_estimate_by_custom_field(body: CustomFieldEstimateUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, None, body.model_dump(exclude_none=True), user, LABEL,
                            custom_field=body.custom_field, custom_value=body.value)


# ---------------------------------------------------------------------------
# 2. Update custom fields only
# ---------------------------------------------------------------------------

@router.put("/{estimate_id}/customfields")
async def update_custom_fields(estimate_id: UUID, body: CustomFieldUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, estimate_id, {"custom_fields": body.custom_fields}, user, LABEL)


# ---------------------------------------------------------------------------
# 3. Submit estimate
# ---------------------------------------------------------------------------

@router.post("/{estimate_id}/submit")
async def submit_estimate(estimate_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, estimate_id, "submitted", user, LABEL)


# ---------------------------------------------------------------------------
# 4. Approve estimate
# ---------------------------------------------------------------------------

@router.post("/{estimate_id}/approve")
async def approve_estimate(estimate_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, estimate_id, "approved", user, LABEL)


# ---------------------------------------------------------------------------
# 5. Get email content
# ---------------------------------------------------------------------------

@router.get("/{estimate_id}/email")
async def get_email_content(estimate_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_email_get(TABLE, PK, estimate_id, user, LABEL)


# ---------------------------------------------------------------------------
# 6. Email estimate
# ---------------------------------------------------------------------------

@router.post("/{estimate_id}/email")
async def send_estimate_email(estimate_id: UUID, body: EmailInput, user: UserContext = Depends(_auth)):
    return await acc_email_send(TABLE, PK, estimate_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 7. Email multiple estimates
# ---------------------------------------------------------------------------

@router.post("/email")
async def bulk_email_estimates(body: BulkEmailInput, user: UserContext = Depends(_auth)):
    results = []
    for est_id in body.estimate_ids:
        r = await acc_email_send(TABLE, PK, est_id, {}, user, LABEL)
        results.append(r)
    return {"message": f"Emails sent for {len(results)} estimate(s)", "results": results}


# ---------------------------------------------------------------------------
# 8. Single estimate PDF
# ---------------------------------------------------------------------------

@router.get("/{estimate_id}/pdf")
async def get_estimate_pdf(estimate_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_document_pdf
    pdf_bytes, filename = await generate_document_pdf("estimate", estimate_id, user)
    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{filename}"'})


# ---------------------------------------------------------------------------
# 9. Bulk export PDF (ZIP)
# ---------------------------------------------------------------------------

@router.get("/pdf")
async def bulk_export_pdf(
    estimate_ids: Optional[str] = Query(None, description="Comma-separated estimate IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_bulk_pdf
    ids = [UUID(i.strip()) for i in estimate_ids.split(",") if i.strip()] if estimate_ids else []
    if not ids:
        return {"message": "No estimate_ids provided"}
    zip_bytes, filename = await generate_bulk_pdf("estimate", ids, user)
    return Response(content=zip_bytes, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------------------
# 10. Single estimate print (HTML)
# ---------------------------------------------------------------------------

@router.get("/{estimate_id}/print")
async def get_estimate_print(estimate_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_print_html
    html = await generate_print_html("estimate", estimate_id, user)
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# 11. Bulk print
# ---------------------------------------------------------------------------

@router.get("/print")
async def bulk_print(
    estimate_ids: Optional[str] = Query(None, description="Comma-separated estimate IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_print_html
    ids = [UUID(i.strip()) for i in estimate_ids.split(",") if i.strip()] if estimate_ids else []
    if not ids:
        return {"message": "No estimate_ids provided"}
    pages = []
    for doc_id in ids:
        try:
            pages.append(await generate_print_html("estimate", doc_id, user))
        except ValueError:
            continue
    combined = '<div style="page-break-after:always;"></div>'.join(pages)
    return HTMLResponse(content=combined)


# ---------------------------------------------------------------------------
# 10. Update billing address
# ---------------------------------------------------------------------------

@router.put("/{estimate_id}/address/billing")
async def update_billing_address(estimate_id: UUID, body: AddressInput, user: UserContext = Depends(_auth)):
    return await acc_address_update(TABLE, PK, estimate_id, "billing", body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 11. Update shipping address
# ---------------------------------------------------------------------------

@router.put("/{estimate_id}/address/shipping")
async def update_shipping_address(estimate_id: UUID, body: AddressInput, user: UserContext = Depends(_auth)):
    return await acc_address_update(TABLE, PK, estimate_id, "shipping", body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 12. List templates
# ---------------------------------------------------------------------------

@router.get("/templates")
async def list_templates(user: UserContext = Depends(_auth)):
    return await acc_templates_list(TABLE, user)


# ---------------------------------------------------------------------------
# 13. Update template
# ---------------------------------------------------------------------------

@router.put("/{estimate_id}/templates/{template_id}")
async def update_template(estimate_id: UUID, template_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_template_update(TABLE, PK, estimate_id, template_id, user, LABEL)


# ---------------------------------------------------------------------------
# 14. List comments
# ---------------------------------------------------------------------------

@router.get("/{estimate_id}/comments")
async def list_comments(estimate_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comments_list(TABLE, PK, estimate_id, user, LABEL)


# ---------------------------------------------------------------------------
# 15. Add comment
# ---------------------------------------------------------------------------

@router.post("/{estimate_id}/comments", status_code=201)
async def add_comment(estimate_id: UUID, body: CommentInput, user: UserContext = Depends(_auth)):
    return await acc_comment_add(TABLE, PK, estimate_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 16. Update comment
# ---------------------------------------------------------------------------

@router.put("/{estimate_id}/comments/{comment_id}")
async def update_comment(estimate_id: UUID, comment_id: UUID, body: CommentInput, user: UserContext = Depends(_auth)):
    return await acc_comment_update(TABLE, PK, estimate_id, comment_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 17. Delete comment
# ---------------------------------------------------------------------------

@router.delete("/{estimate_id}/comments/{comment_id}")
async def delete_comment(estimate_id: UUID, comment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comment_delete(TABLE, PK, estimate_id, comment_id, user, LABEL)
