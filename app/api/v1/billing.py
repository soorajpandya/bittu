"""Billing & Invoice endpoints (read-only)."""
from fastapi import APIRouter, Depends, Query

from app.core.auth import UserContext, require_role
from app.services.billing_service import BillingService

router = APIRouter(prefix="/billing", tags=["Billing"])
_svc = BillingService()


@router.get("/history")
async def list_billing_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.list_billing_history(user, limit=limit, offset=offset)


@router.get("/history/{record_id}")
async def get_billing_record(
    record_id: int,
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.get_billing_record(user, record_id)


@router.get("/invoices")
async def list_invoices(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.list_invoices(user, limit=limit, offset=offset)


@router.get("/invoices/{invoice_id}")
async def get_invoice(
    invoice_id: int,
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.get_invoice(user, invoice_id)
