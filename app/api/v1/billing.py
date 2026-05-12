"""Billing & Invoice endpoints (read-only)."""
from fastapi import APIRouter, Depends, Query

from app.core.auth import UserContext, require_permission
from app.services.billing_service import BillingService

router = APIRouter(prefix="/billing", tags=["Billing"])
_svc = BillingService()


@router.get("/invoices")
async def list_invoices(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("billing.read")),
):
    return await _svc.list_invoices(user, limit=limit, offset=offset)


@router.get("/invoices/{invoice_id}")
async def get_invoice(
    invoice_id: int,
    user: UserContext = Depends(require_permission("billing.read")),
):
    return await _svc.get_invoice(user, invoice_id)
