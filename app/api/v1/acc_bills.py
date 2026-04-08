"""Bills CRUD endpoints (vendor invoices)."""
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
    acc_attachment_get, acc_attachment_add, acc_attachment_delete,
    acc_address_update, acc_sub_list, acc_sub_create, acc_sub_delete,
)

router = APIRouter(prefix="/accounting/bills", tags=["Accounting – Bills"])

TABLE = "acc_bills"
PK = "bill_id"
LABEL = "Bill"
LINE_PARENT = "bill"


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
    customer_id: Optional[UUID] = None


class BillCreate(BaseModel):
    vendor_id: UUID
    bill_number: Optional[str] = None
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
    adjustment: float = 0
    adjustment_description: Optional[str] = None
    purchase_order_id: Optional[UUID] = None
    payment_terms: Optional[int] = None
    payment_terms_label: Optional[str] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    line_items: Optional[list[LineItem]] = None


class BillUpdate(BaseModel):
    vendor_id: Optional[UUID] = None
    bill_number: Optional[str] = None
    reference_number: Optional[str] = None
    date: Optional[str] = None
    due_date: Optional[str] = None
    currency_code: Optional[str] = None
    exchange_rate: Optional[float] = None
    notes: Optional[str] = None
    terms: Optional[str] = None
    discount: Optional[float] = None
    discount_type: Optional[str] = None
    adjustment: Optional[float] = None
    adjustment_description: Optional[str] = None
    purchase_order_id: Optional[UUID] = None
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
async def list_bills(
    user: UserContext = Depends(_auth),
    vendor_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"vendor_id": vendor_id, "status": status}, page=page, per_page=per_page, search_fields=["bill_number", "reference_number"])


@router.post("", status_code=201)
async def create_bill(body: BillCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    items = [li.model_dump() for li in (body.line_items or [])]
    data.pop("line_items", None)
    data = _calc_totals(data, items)
    row = await acc_create(TABLE, data, user)
    if items:
        await acc_line_items_create(row[PK], LINE_PARENT, items, user)
    row["line_items"] = await acc_line_items_get(row[PK], LINE_PARENT, user)
    return row


@router.get("/{bill_id}")
async def get_bill(bill_id: UUID, user: UserContext = Depends(_auth)):
    row = await acc_get(TABLE, PK, bill_id, user, LABEL)
    row["line_items"] = await acc_line_items_get(bill_id, LINE_PARENT, user)
    return row


@router.put("/{bill_id}")
async def update_bill(bill_id: UUID, body: BillUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    items = None
    if body.line_items is not None:
        items = [li.model_dump() for li in body.line_items]
        data.pop("line_items", None)
        data = _calc_totals(data, items)
    row = await acc_update(TABLE, PK, bill_id, data, user, LABEL)
    if items is not None:
        await acc_line_items_replace(bill_id, LINE_PARENT, items, user)
    row["line_items"] = await acc_line_items_get(bill_id, LINE_PARENT, user)
    return row


@router.delete("/{bill_id}")
async def delete_bill(bill_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, bill_id, user, LABEL)


@router.post("/{bill_id}/status/open")
async def mark_open(bill_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, bill_id, "open", user, LABEL)


@router.post("/{bill_id}/status/void")
async def mark_void(bill_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, bill_id, "void", user, LABEL)


# ---------------------------------------------------------------------------
# Pydantic models for new endpoints
# ---------------------------------------------------------------------------

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
    description: Optional[str] = None


class CommentInput(BaseModel):
    description: str
    show_comment_to_clients: Optional[bool] = False


class CreditApplyInput(BaseModel):
    vendor_credit_id: UUID
    amount: float


class CustomFieldUpdate(BaseModel):
    custom_fields: list


class CustomFieldBillUpdate(BaseModel):
    custom_field: str
    value: str
    data: dict


# ---------------------------------------------------------------------------
# 1. Update bill via custom field
# ---------------------------------------------------------------------------

@router.put("/update")
async def update_bill_by_custom_field(body: CustomFieldBillUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, None, body.model_dump(exclude_none=True), user, LABEL,
                            custom_field=body.custom_field, custom_value=body.value)


# ---------------------------------------------------------------------------
# 2. Update custom fields only
# ---------------------------------------------------------------------------

@router.put("/{bill_id}/customfields")
async def update_custom_fields(bill_id: UUID, body: CustomFieldUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, bill_id, {"custom_fields": body.custom_fields}, user, LABEL)


# ---------------------------------------------------------------------------
# 3. Submit bill
# ---------------------------------------------------------------------------

@router.post("/{bill_id}/submit")
async def submit_bill(bill_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, bill_id, "submitted", user, LABEL)


# ---------------------------------------------------------------------------
# 4. Approve bill
# ---------------------------------------------------------------------------

@router.post("/{bill_id}/approve")
async def approve_bill(bill_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, bill_id, "approved", user, LABEL)


# ---------------------------------------------------------------------------
# 5. Update billing address
# ---------------------------------------------------------------------------

@router.put("/{bill_id}/address/billing")
async def update_billing_address(bill_id: UUID, body: AddressInput, user: UserContext = Depends(_auth)):
    return await acc_address_update(TABLE, PK, bill_id, "billing", body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 6. List bill payments
# ---------------------------------------------------------------------------

@router.get("/{bill_id}/payments")
async def list_payments(bill_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_list("acc_bill_payments", "bill_id", bill_id, user)


# ---------------------------------------------------------------------------
# 7. Apply credits to bill
# ---------------------------------------------------------------------------

@router.post("/{bill_id}/credits", status_code=201)
async def apply_credit(bill_id: UUID, body: CreditApplyInput, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    data["bill_id"] = str(bill_id)
    return await acc_sub_create("acc_bill_credits", data, user)


# ---------------------------------------------------------------------------
# 8. Delete bill payment
# ---------------------------------------------------------------------------

@router.delete("/{bill_id}/payments/{bill_payment_id}")
async def delete_payment(bill_id: UUID, bill_payment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_delete("acc_bill_payments", "bill_payment_id", bill_payment_id, user)


# ---------------------------------------------------------------------------
# 9. Get attachments
# ---------------------------------------------------------------------------

@router.get("/{bill_id}/attachment")
async def get_attachment(bill_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_get(TABLE, PK, bill_id, user, LABEL)


# ---------------------------------------------------------------------------
# 10. Add attachment
# ---------------------------------------------------------------------------

@router.post("/{bill_id}/attachment", status_code=201)
async def add_attachment(bill_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_add(TABLE, PK, bill_id, user, LABEL)


# ---------------------------------------------------------------------------
# 11. Delete attachment
# ---------------------------------------------------------------------------

@router.delete("/{bill_id}/attachment")
async def delete_attachment(bill_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_delete(TABLE, PK, bill_id, user, LABEL)


# ---------------------------------------------------------------------------
# 12. List comments
# ---------------------------------------------------------------------------

@router.get("/{bill_id}/comments")
async def list_comments(bill_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comments_list(TABLE, PK, bill_id, user, LABEL)


# ---------------------------------------------------------------------------
# 13. Add comment
# ---------------------------------------------------------------------------

@router.post("/{bill_id}/comments", status_code=201)
async def add_comment(bill_id: UUID, body: CommentInput, user: UserContext = Depends(_auth)):
    return await acc_comment_add(TABLE, PK, bill_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 14. Delete comment
# ---------------------------------------------------------------------------

@router.delete("/{bill_id}/comments/{comment_id}")
async def delete_comment(bill_id: UUID, comment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comment_delete(TABLE, PK, bill_id, comment_id, user, LABEL)


# ---------------------------------------------------------------------------
# 15. Convert purchase order to bill (edit page)
# ---------------------------------------------------------------------------

@router.get("/editpage/frompurchaseorders")
async def from_purchase_orders(
    purchaseorder_id: UUID = Query(...),
    user: UserContext = Depends(_auth),
):
    po = await acc_get("acc_purchase_orders", "purchaseorder_id", purchaseorder_id, user, "Purchase Order")
    return {
        "vendor_id": po.get("vendor_id"),
        "purchase_order_id": str(purchaseorder_id),
        "currency_code": po.get("currency_code"),
        "exchange_rate": po.get("exchange_rate", 1.0),
        "line_items": po.get("line_items", []),
        "notes": po.get("notes"),
        "terms": po.get("terms"),
    }


# ---------------------------------------------------------------------------
# PDF & Print endpoints
# ---------------------------------------------------------------------------

@router.get("/{bill_id}/pdf")
async def get_bill_pdf(bill_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_document_pdf
    pdf_bytes, filename = await generate_document_pdf("bill", bill_id, user)
    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{filename}"'})


@router.get("/pdf")
async def bulk_export_bill_pdf(
    bill_ids: Optional[str] = Query(None, description="Comma-separated IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_bulk_pdf
    ids = [UUID(i.strip()) for i in bill_ids.split(",") if i.strip()] if bill_ids else []
    if not ids:
        return {"message": "No IDs provided"}
    zip_bytes, filename = await generate_bulk_pdf("bill", ids, user)
    return Response(content=zip_bytes, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{bill_id}/print")
async def get_bill_print(bill_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_print_html
    html = await generate_print_html("bill", bill_id, user)
    return HTMLResponse(content=html)


@router.get("/print")
async def bulk_print_bills(
    bill_ids: Optional[str] = Query(None, description="Comma-separated IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_print_html
    ids = [UUID(i.strip()) for i in bill_ids.split(",") if i.strip()] if bill_ids else []
    if not ids:
        return {"message": "No IDs provided"}
    pages = []
    for doc_id in ids:
        try:
            pages.append(await generate_print_html("bill", doc_id, user))
        except ValueError:
            continue
    combined = '<div style="page-break-after:always;"></div>'.join(pages)
    return HTMLResponse(content=combined)
