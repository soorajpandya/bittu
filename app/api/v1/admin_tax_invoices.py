"""
Tax Invoices — Admin API (Phase 5).

Prefix:   /admin/tax-invoices
Audience: platform admins (membership in ``platform_admin_users``).
Every endpoint is gated by :func:`require_platform_admin`.
Cross-merchant visibility — admin can create / issue / cancel for any
merchant.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_platform_admin
from app.core.logging import get_logger
from app.services.tax_invoice_service import tax_invoice_service

router = APIRouter(prefix="/admin/tax-invoices", tags=["Tax Invoices (Admin)"])
logger = get_logger(__name__)


class CreateDraftBody(BaseModel):
    merchant_id:      str
    branch_id:        Optional[str] = None
    invoice_date:     Optional[date] = None
    period_start:     Optional[date] = None
    period_end:       Optional[date] = None
    due_date:         Optional[date] = None
    currency:         str = Field("INR", min_length=3, max_length=3)
    place_of_supply:  Optional[str] = None
    gstin_supplier:   Optional[str] = None
    gstin_customer:   Optional[str] = None
    supplier_name:    Optional[str] = None
    supplier_address: Optional[str] = None
    customer_name:    Optional[str] = None
    customer_address: Optional[str] = None
    notes:            Optional[str] = None
    metadata:         Optional[dict] = None


class AddLineBody(BaseModel):
    description:     str = Field(..., min_length=1, max_length=500)
    hsn_sac:         Optional[str] = Field(None, max_length=20)
    quantity:        float = Field(1, gt=0)
    unit_amount:     float = Field(0, ge=0)
    discount_amount: float = Field(0, ge=0)
    cgst_rate:       float = Field(0, ge=0, le=100)
    sgst_rate:       float = Field(0, ge=0, le=100)
    igst_rate:       float = Field(0, ge=0, le=100)
    cess_rate:       float = Field(0, ge=0, le=100)
    metadata:        Optional[dict] = None


class RejectBody(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)


@router.post("/", status_code=201)
async def create_draft(
    body: CreateDraftBody,
    user: UserContext = Depends(require_platform_admin()),
):
    return await tax_invoice_service.create_draft(
        merchant_id=body.merchant_id,
        branch_id=body.branch_id,
        invoice_date=body.invoice_date,
        period_start=body.period_start, period_end=body.period_end,
        due_date=body.due_date, currency=body.currency,
        place_of_supply=body.place_of_supply,
        gstin_supplier=body.gstin_supplier, gstin_customer=body.gstin_customer,
        supplier_name=body.supplier_name, supplier_address=body.supplier_address,
        customer_name=body.customer_name, customer_address=body.customer_address,
        notes=body.notes, metadata=body.metadata,
        created_by=user.user_id,
    )


@router.post("/{invoice_id}/lines", status_code=201)
async def add_line(
    invoice_id: str,
    body: AddLineBody,
    user: UserContext = Depends(require_platform_admin()),
):
    return await tax_invoice_service.add_line(
        invoice_id=invoice_id,
        description=body.description, hsn_sac=body.hsn_sac,
        quantity=body.quantity, unit_amount=body.unit_amount,
        discount_amount=body.discount_amount,
        cgst_rate=body.cgst_rate, sgst_rate=body.sgst_rate,
        igst_rate=body.igst_rate, cess_rate=body.cess_rate,
        metadata=body.metadata,
    )


@router.delete("/{invoice_id}/lines/{line_id}")
async def remove_line(
    invoice_id: str, line_id: str,
    user: UserContext = Depends(require_platform_admin()),
):
    return await tax_invoice_service.remove_line(
        invoice_id=invoice_id, line_id=line_id,
    )


@router.post("/{invoice_id}/issue")
async def issue(
    invoice_id: str,
    user: UserContext = Depends(require_platform_admin()),
):
    return await tax_invoice_service.issue(
        invoice_id=invoice_id, actor_id=user.user_id,
    )


@router.post("/{invoice_id}/cancel")
async def cancel(
    invoice_id: str,
    body: RejectBody,
    user: UserContext = Depends(require_platform_admin()),
):
    return await tax_invoice_service.cancel(
        invoice_id=invoice_id, actor_id=user.user_id, reason=body.reason,
    )


@router.get("/")
async def list_invoices(
    merchant_id: Optional[str] = Query(None),
    status:      Optional[str] = Query(None, pattern=r"^(draft|issued|cancelled)$"),
    from_date:   Optional[date] = Query(None),
    to_date:     Optional[date] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    user: UserContext = Depends(require_platform_admin()),
):
    return await tax_invoice_service.list_invoices(
        merchant_id=merchant_id, status=status,
        from_date=from_date, to_date=to_date, limit=limit,
    )


@router.get("/{invoice_id}")
async def get_invoice(
    invoice_id: str,
    user: UserContext = Depends(require_platform_admin()),
):
    return await tax_invoice_service.get_invoice(
        invoice_id=invoice_id, include_lines=True,
    )


@router.get("/{invoice_id}/csv")
async def download_csv(
    invoice_id: str,
    download: bool = Query(True),
    user: UserContext = Depends(require_platform_admin()),
):
    out = await tax_invoice_service.to_csv(invoice_id=invoice_id)
    if download:
        return Response(
            content=out["file_content"],
            media_type="text/csv",
            headers={
                "Content-Disposition":
                    f'attachment; filename="{out["file_name"]}"',
            },
        )
    return out
