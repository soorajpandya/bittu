"""
AI-Powered Sales Invoice Import endpoints.

Flow:
  POST /parse          → Upload image, get parsed preview (no DB save)
  POST /parse/upload   → Same but multipart file upload
  POST /confirm        → Save parsed data → inventory + accounting
  GET  /               → List invoices
  GET  /{invoice_id}   → Get invoice detail with line items
"""
import base64
from typing import Optional

from fastapi import APIRouter, Depends, UploadFile, File
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.services.invoice_import_service import InvoiceImportService

router = APIRouter(prefix="/invoice-import", tags=["Invoice Import"])
_svc = InvoiceImportService()


# ── Request / Response models ──

class ParseInvoiceBase64In(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"
    idempotency_key: Optional[str] = None


class InvoiceItemIn(BaseModel):
    name: str
    ingredient_id: Optional[str] = None
    match_status: Optional[str] = None
    hsn_code: Optional[str] = None
    quantity: float = 0
    unit: str = "pcs"
    unit_price: float = 0
    discount_percent: float = 0
    tax_percent: float = 0
    tax_amount: float = 0
    line_total: float = 0


class ConfirmInvoiceIn(BaseModel):
    vendor_name: Optional[str] = None
    vendor_gstin: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    items: list[InvoiceItemIn]
    subtotal: Optional[float] = None
    tax_amount: Optional[float] = None
    total_amount: float = 0
    payment_status: str = "unpaid"
    notes: Optional[str] = None
    idempotency_key: Optional[str] = None
    ocr_text: Optional[str] = None
    purchase_order_id: Optional[str] = None


# ── Endpoints ──

@router.post("/parse")
async def parse_invoice_base64(
    body: ParseInvoiceBase64In,
    user: UserContext = Depends(require_permission("inventory.manage")),
):
    """
    Parse a sales invoice image (base64) using OCR + AI.
    Returns structured data for preview. Does NOT save to DB.
    """
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await _svc.parse_invoice(
        image_base64=body.image_base64,
        mime_type=body.mime_type,
        idempotency_key=body.idempotency_key,
        user_id=uid,
    )


@router.post("/parse/upload")
async def parse_invoice_upload(
    file: UploadFile = File(...),
    user: UserContext = Depends(require_permission("inventory.manage")),
):
    """
    Parse a sales invoice uploaded as multipart file (image/PDF).
    Returns structured data for preview. Does NOT save to DB.
    """
    content = await file.read()
    image_b64 = base64.b64encode(content).decode()
    mime = file.content_type or "image/jpeg"
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await _svc.parse_invoice(
        image_base64=image_b64,
        mime_type=mime,
        user_id=uid,
    )


@router.post("/confirm")
async def confirm_invoice(
    body: ConfirmInvoiceIn,
    user: UserContext = Depends(require_permission("inventory.manage")),
):
    """
    Confirm and save parsed invoice data.
    Creates: purchase_invoice, inventory updates, expense record.
    Frontend sends the (potentially edited) parsed data back.
    """
    uid = user.owner_id if user.is_branch_user else user.user_id
    restaurant_id = getattr(user, "restaurant_id", None)

    return await _svc.confirm_invoice(
        user_id=uid,
        restaurant_id=restaurant_id,
        branch_id=user.branch_id,
        parsed_data=body.model_dump(),
        purchase_order_id=body.purchase_order_id,
    )


@router.get("/")
async def list_invoices(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    user: UserContext = Depends(require_permission("inventory.read")),
):
    """List all imported invoices for the current tenant."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await _svc.list_invoices(user_id=uid, status=status, limit=limit, offset=offset)


@router.get("/{invoice_id}")
async def get_invoice(
    invoice_id: str,
    user: UserContext = Depends(require_permission("inventory.read")),
):
    """Get a specific invoice with line items."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await _svc.get_invoice(user_id=uid, invoice_id=invoice_id)
