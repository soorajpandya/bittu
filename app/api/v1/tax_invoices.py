"""
Tax Invoices — Merchant API (Phase 5).

Prefix:   /tax-invoices
Audience: merchants viewing platform-issued tax invoices for their own
account. Merchants are READ-ONLY here; creation/issuance/cancellation
happens via ``/admin/tax-invoices``.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response

from app.core.auth import UserContext, require_permission
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.services.tax_invoice_service import tax_invoice_service

router = APIRouter(prefix="/tax-invoices", tags=["Tax Invoices"])
logger = get_logger(__name__)


def _merchant_id(user: UserContext) -> str:
    if not user.restaurant_id:
        raise ValidationError("Active restaurant context is required.")
    return str(user.restaurant_id)


@router.get("/")
async def list_invoices(
    status: Optional[str] = Query(None, pattern=r"^(draft|issued|cancelled)$"),
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    user: UserContext = Depends(require_permission("invoice.read")),
):
    return await tax_invoice_service.list_invoices(
        merchant_id=_merchant_id(user),
        status=status, from_date=from_date, to_date=to_date, limit=limit,
    )


@router.get("/{invoice_id}")
async def get_invoice(
    invoice_id: str,
    user: UserContext = Depends(require_permission("invoice.read")),
):
    return await tax_invoice_service.get_invoice(
        invoice_id=invoice_id,
        merchant_id=_merchant_id(user),
        include_lines=True,
    )


@router.get("/{invoice_id}/lines")
async def list_lines(
    invoice_id: str,
    user: UserContext = Depends(require_permission("invoice.read")),
):
    # Enforce merchant scope by loading the invoice first.
    await tax_invoice_service.get_invoice(
        invoice_id=invoice_id,
        merchant_id=_merchant_id(user),
        include_lines=False,
    )
    return await tax_invoice_service.list_lines(invoice_id)


@router.get("/{invoice_id}/csv")
async def download_csv(
    invoice_id: str,
    download: bool = Query(True),
    user: UserContext = Depends(require_permission("invoice.read")),
):
    out = await tax_invoice_service.to_csv(
        invoice_id=invoice_id,
        merchant_id=_merchant_id(user),
    )
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
