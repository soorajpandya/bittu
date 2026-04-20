"""Sub-ledger (AR/AP) and Tax Liability API endpoints."""
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.core.logging import get_logger
from app.services.subledger_service import subledger_service
from app.services.tax_service import tax_service

subledger_router = APIRouter(prefix="/subledger", tags=["Sub-Ledger"])
tax_router = APIRouter(prefix="/tax", tags=["Tax Liability"])
logger = get_logger(__name__)


# ── Sub-Ledger Endpoints ─────────────────────────────────────────────────────

@subledger_router.get("/ar/balances")
async def ar_balances(
    user: UserContext = Depends(require_permission("subledger.read")),
):
    """Get all customer balances (AR)."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await subledger_service.all_customer_balances(uid)


@subledger_router.get("/ar/aging")
async def ar_aging(
    customer_id: Optional[str] = Query(None),
    as_of: Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("subledger.read")),
):
    """AR aging report — 30/60/90+ days buckets."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await subledger_service.customer_aging(uid, customer_id, as_of)


@subledger_router.get("/ar/{customer_id}")
async def customer_ledger(
    customer_id: str,
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("subledger.read")),
):
    """Get customer ledger entries."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await subledger_service.get_customer_ledger(
        uid, customer_id, from_date, to_date, limit, offset,
    )


@subledger_router.get("/ar/{customer_id}/balance")
async def customer_balance(
    customer_id: str,
    user: UserContext = Depends(require_permission("subledger.read")),
):
    """Get a single customer's AR balance."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    bal = await subledger_service.get_customer_balance(uid, customer_id)
    return {"customer_id": customer_id, "balance": bal}


@subledger_router.get("/ap/balances")
async def ap_balances(
    user: UserContext = Depends(require_permission("subledger.read")),
):
    """Get all supplier balances (AP)."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await subledger_service.all_supplier_balances(uid)


@subledger_router.get("/ap/aging")
async def ap_aging(
    supplier_id: Optional[str] = Query(None),
    as_of: Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("subledger.read")),
):
    """AP aging report — 30/60/90+ days buckets."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await subledger_service.supplier_aging(uid, supplier_id, as_of)


@subledger_router.get("/ap/{supplier_id}")
async def supplier_ledger(
    supplier_id: str,
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("subledger.read")),
):
    """Get supplier ledger entries."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await subledger_service.get_supplier_ledger(
        uid, supplier_id, from_date, to_date, limit, offset,
    )


@subledger_router.get("/ap/{supplier_id}/balance")
async def supplier_balance(
    supplier_id: str,
    user: UserContext = Depends(require_permission("subledger.read")),
):
    """Get a single supplier's AP balance."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    bal = await subledger_service.get_supplier_balance(uid, supplier_id)
    return {"supplier_id": supplier_id, "balance": bal}


# ── Tax Liability Endpoints ──────────────────────────────────────────────────

class TaxComputeRequest(BaseModel):
    period_start: date
    period_end: date
    period_label: Optional[str] = None


class TaxPaymentRequest(BaseModel):
    payment_method: str = "bank"
    payment_reference: str = ""


@tax_router.post("/compute")
async def compute_tax_liability(
    body: TaxComputeRequest,
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("tax.compute")),
):
    """Compute GST liability for a period from journal entries."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await tax_service.compute_liability(
        restaurant_id=uid,
        period_start=body.period_start,
        period_end=body.period_end,
        branch_id=branch_id,
        period_label=body.period_label,
        created_by=user.user_id,
    )


@tax_router.get("")
async def list_tax_liabilities(
    status: Optional[str] = Query(None),
    limit: int = Query(20, le=100),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("tax.read")),
):
    """List all tax liability periods."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await tax_service.list_liabilities(uid, status, limit, offset)


@tax_router.get("/gstr3b")
async def gstr3b_data(
    period_start: date = Query(...),
    period_end: date = Query(...),
    user: UserContext = Depends(require_permission("tax.read")),
):
    """Get GSTR-3B summary data for a period."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await tax_service.gst_return_data(uid, period_start, period_end)


@tax_router.get("/{liability_id}")
async def get_tax_liability(
    liability_id: str,
    user: UserContext = Depends(require_permission("tax.read")),
):
    """Get details of a tax liability period."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await tax_service.get_liability(liability_id, uid)


@tax_router.post("/{liability_id}/file")
async def file_tax_liability(
    liability_id: str,
    user: UserContext = Depends(require_permission("tax.file")),
):
    """Mark tax liability as filed with GST portal."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await tax_service.mark_filed(liability_id, uid)


@tax_router.post("/{liability_id}/pay")
async def pay_tax_liability(
    liability_id: str,
    body: TaxPaymentRequest,
    user: UserContext = Depends(require_permission("tax.file")),
):
    """Record tax payment to government."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await tax_service.record_tax_payment(
        liability_id=liability_id,
        restaurant_id=uid,
        payment_method=body.payment_method,
        payment_reference=body.payment_reference,
        created_by=user.user_id,
    )
