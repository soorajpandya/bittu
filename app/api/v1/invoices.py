"""Invoice management API endpoints."""
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.services.invoice_service import invoice_service

router = APIRouter(prefix="/invoices", tags=["Invoices"])
logger = get_logger(__name__)


class InvoiceItemCreate(BaseModel):
    item_name: str
    hsn_code: str = ""
    quantity: float = 1
    unit_price: float = 0
    discount: float = 0
    cgst_rate: float = 0
    sgst_rate: float = 0
    igst_rate: float = 0


class InvoiceCreate(BaseModel):
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    customer_gstin: Optional[str] = None
    order_id: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    invoice_type: str = "tax_invoice"
    items: list[InvoiceItemCreate] = []
    notes: str = ""
    terms: str = ""


class InvoicePaymentCreate(BaseModel):
    amount: float
    payment_method: str = "cash"
    payment_id: Optional[str] = None


class InvoiceVoidRequest(BaseModel):
    reason: str = ""


@router.post("")
async def create_invoice(
    body: InvoiceCreate,
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("invoice.write")),
):
    """Create a new invoice."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await invoice_service.create_invoice(
        restaurant_id=uid,
        branch_id=branch_id or (user.branch_id if user.is_branch_user else None),
        customer_id=body.customer_id,
        customer_name=body.customer_name,
        customer_gstin=body.customer_gstin,
        order_id=body.order_id,
        invoice_date=body.invoice_date,
        due_date=body.due_date,
        invoice_type=body.invoice_type,
        items=[i.model_dump() for i in body.items],
        notes=body.notes,
        terms=body.terms,
        created_by=user.user_id,
    )


@router.get("")
async def list_invoices(
    status: Optional[str] = Query(None),
    customer_id: Optional[str] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("invoice.read")),
):
    """List invoices with optional filters."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await invoice_service.list_invoices(
        restaurant_id=uid,
        status=status,
        customer_id=customer_id,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        offset=offset,
    )


@router.get("/unpaid")
async def get_unpaid_invoices(
    customer_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("invoice.read")),
):
    """Get all invoices with outstanding balance."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await invoice_service.get_unpaid_invoices(uid, customer_id)


@router.get("/{invoice_id}")
async def get_invoice(
    invoice_id: str,
    user: UserContext = Depends(require_permission("invoice.read")),
):
    """Get invoice with line items."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await invoice_service.get_invoice(invoice_id, uid)


@router.post("/{invoice_id}/payment")
async def record_invoice_payment(
    invoice_id: str,
    body: InvoicePaymentCreate,
    user: UserContext = Depends(require_permission("invoice.write")),
):
    """Record a payment against an invoice."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await invoice_service.record_invoice_payment(
        invoice_id=invoice_id,
        restaurant_id=uid,
        amount=body.amount,
        payment_method=body.payment_method,
        payment_id=body.payment_id,
        created_by=user.user_id,
    )


@router.post("/{invoice_id}/void")
async def void_invoice(
    invoice_id: str,
    body: InvoiceVoidRequest,
    user: UserContext = Depends(require_permission("invoice.void")),
):
    """Void an invoice (must have no payments)."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await invoice_service.void_invoice(
        invoice_id=invoice_id,
        restaurant_id=uid,
        reason=body.reason,
        created_by=user.user_id,
    )
