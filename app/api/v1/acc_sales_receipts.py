"""Sales Receipts CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import (
    acc_list, acc_get, acc_create, acc_update, acc_delete,
    acc_line_items_create, acc_line_items_replace, acc_line_items_get,
)

router = APIRouter(prefix="/accounting/salesreceipts", tags=["Accounting – Sales Receipts"])

TABLE = "acc_sales_receipts"
PK = "sales_receipt_id"
LABEL = "Sales Receipt"


_auth = require_permission("accounting:read")


class LineItemInput(BaseModel):
    item_id: Optional[UUID] = None
    name: Optional[str] = None
    description: Optional[str] = None
    rate: float = 0
    quantity: float = 1
    tax_id: Optional[UUID] = None
    tax_percentage: float = 0
    item_order: int = 0


class SalesReceiptCreate(BaseModel):
    customer_id: UUID
    payment_mode: str
    line_items: list[LineItemInput]
    receipt_number: Optional[str] = None
    date: Optional[str] = None
    deposit_to_account_id: Optional[UUID] = None
    currency_id: Optional[UUID] = None
    exchange_rate: float = 1.0
    notes: Optional[str] = None
    terms: Optional[str] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


class SalesReceiptUpdate(BaseModel):
    customer_id: Optional[UUID] = None
    payment_mode: Optional[str] = None
    line_items: Optional[list[LineItemInput]] = None
    receipt_number: Optional[str] = None
    date: Optional[str] = None
    deposit_to_account_id: Optional[UUID] = None
    currency_id: Optional[UUID] = None
    exchange_rate: Optional[float] = None
    notes: Optional[str] = None
    terms: Optional[str] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


def _calc(items):
    s = sum(i.rate * i.quantity for i in items)
    t = sum(i.rate * i.quantity * i.tax_percentage / 100 for i in items)
    return {"sub_total": round(s, 2), "tax_total": round(t, 2), "total": round(s + t, 2)}


@router.get("")
async def list_sales_receipts(user: UserContext = Depends(_auth), customer_id: Optional[UUID] = Query(None), page: int = Query(1, ge=1), per_page: int = Query(25, ge=1, le=200)):
    return await acc_list(TABLE, user, filters={"customer_id": customer_id}, page=page, per_page=per_page, order_by="date DESC")


@router.post("", status_code=201)
async def create_sales_receipt(body: SalesReceiptCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude={"line_items"}, exclude_none=True)
    data.update(_calc(body.line_items))
    sr = await acc_create(TABLE, data, user)
    sr["line_items"] = await acc_line_items_create(sr[PK], "sales_receipt", [li.model_dump(exclude_none=True) for li in body.line_items], user)
    return sr


@router.get("/{sales_receipt_id}")
async def get_sales_receipt(sales_receipt_id: UUID, user: UserContext = Depends(_auth)):
    sr = await acc_get(TABLE, PK, sales_receipt_id, user, LABEL)
    sr["line_items"] = await acc_line_items_get(sales_receipt_id, "sales_receipt", user)
    return sr


@router.put("/{sales_receipt_id}")
async def update_sales_receipt(sales_receipt_id: UUID, body: SalesReceiptUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude={"line_items"}, exclude_unset=True, exclude_none=True)
    if body.line_items is not None:
        data.update(_calc(body.line_items))
    sr = await acc_update(TABLE, PK, sales_receipt_id, data, user, LABEL)
    if body.line_items is not None:
        sr["line_items"] = await acc_line_items_replace(sales_receipt_id, "sales_receipt", [li.model_dump(exclude_none=True) for li in body.line_items], user)
    return sr


@router.delete("/{sales_receipt_id}")
async def delete_sales_receipt(sales_receipt_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, sales_receipt_id, user, LABEL)


# ---------------------------------------------------------------------------
# PDF & Print endpoints
# ---------------------------------------------------------------------------

@router.get("/{salesreceipt_id}/pdf")
async def get_salesreceipt_pdf(salesreceipt_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_document_pdf
    pdf_bytes, filename = await generate_document_pdf("salesreceipt", salesreceipt_id, user)
    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{filename}"'})


@router.get("/pdf")
async def bulk_export_salesreceipt_pdf(
    salesreceipt_ids: Optional[str] = Query(None, description="Comma-separated IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_bulk_pdf
    ids = [UUID(i.strip()) for i in salesreceipt_ids.split(",") if i.strip()] if salesreceipt_ids else []
    if not ids:
        return {"message": "No IDs provided"}
    zip_bytes, filename = await generate_bulk_pdf("salesreceipt", ids, user)
    return Response(content=zip_bytes, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{salesreceipt_id}/print")
async def get_salesreceipt_print(salesreceipt_id: UUID, user: UserContext = Depends(_auth)):
    from app.services.pdf_service import generate_print_html
    html = await generate_print_html("salesreceipt", salesreceipt_id, user)
    return HTMLResponse(content=html)


@router.get("/print")
async def bulk_print_salesreceipts(
    salesreceipt_ids: Optional[str] = Query(None, description="Comma-separated IDs"),
    user: UserContext = Depends(_auth),
):
    from app.services.pdf_service import generate_print_html
    ids = [UUID(i.strip()) for i in salesreceipt_ids.split(",") if i.strip()] if salesreceipt_ids else []
    if not ids:
        return {"message": "No IDs provided"}
    pages = []
    for doc_id in ids:
        try:
            pages.append(await generate_print_html("salesreceipt", doc_id, user))
        except ValueError:
            continue
    combined = '<div style="page-break-after:always;"></div>'.join(pages)
    return HTMLResponse(content=combined)
