"""
Bank Reconciliation API — Import statements, auto/manual match, summary.
"""
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, Query, UploadFile, File
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.core.logging import get_logger
from app.services.bank_recon_service import bank_recon_service

router = APIRouter(prefix="/bank-recon", tags=["Bank Reconciliation"])
logger = get_logger(__name__)


# ── Schemas ──────────────────────────────────────────────────────────────────

class StatementLineCreate(BaseModel):
    statement_date: date
    description: str
    amount: float
    reference: str = ""
    bank_account: str = ""
    transaction_type: str = ""
    value_date: Optional[date] = None


class ManualMatchRequest(BaseModel):
    bank_statement_id: str
    journal_entry_id: str
    notes: str = ""


class UnmatchRequest(BaseModel):
    bank_statement_id: str
    journal_entry_id: str


# ══════════════════════════════════════════════════════════════════════════════
# IMPORT
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/import-csv")
async def import_csv(
    file: UploadFile = File(...),
    bank_account: str = Query("", description="Bank account identifier"),
    user: UserContext = Depends(require_permission("bank_recon.write")),
):
    """Import bank statement lines from a CSV file."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    content = (await file.read()).decode("utf-8")
    import uuid as _uuid
    batch_id = str(_uuid.uuid4())[:8]
    return await bank_recon_service.import_statements_csv(
        restaurant_id=uid,
        csv_content=content,
        bank_account=bank_account,
        import_batch_id=batch_id,
    )


@router.post("/statements")
async def add_statement_line(
    body: StatementLineCreate,
    user: UserContext = Depends(require_permission("bank_recon.write")),
):
    """Add a single bank statement line manually."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await bank_recon_service.add_statement_line(
        restaurant_id=uid,
        statement_date=body.statement_date,
        description=body.description,
        amount=body.amount,
        reference=body.reference,
        bank_account=body.bank_account,
        transaction_type=body.transaction_type,
        value_date=body.value_date,
    )


# ══════════════════════════════════════════════════════════════════════════════
# MATCHING
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/auto-match")
async def auto_match(
    date_tolerance: int = Query(2, ge=0, le=7, description="Date tolerance in days"),
    user: UserContext = Depends(require_permission("bank_recon.write")),
):
    """Run auto-matching on all unmatched bank statement lines."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await bank_recon_service.auto_match(
        restaurant_id=uid,
        date_tolerance_days=date_tolerance,
        matched_by=user.user_id,
    )


@router.post("/match")
async def manual_match(
    body: ManualMatchRequest,
    user: UserContext = Depends(require_permission("bank_recon.write")),
):
    """Manually match a bank statement line to a journal entry."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await bank_recon_service.manual_match(
        restaurant_id=uid,
        bank_statement_id=body.bank_statement_id,
        journal_entry_id=body.journal_entry_id,
        matched_by=user.user_id,
        notes=body.notes,
    )


@router.post("/unmatch")
async def unmatch(
    body: UnmatchRequest,
    user: UserContext = Depends(require_permission("bank_recon.write")),
):
    """Remove a reconciliation match."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await bank_recon_service.unmatch(
        restaurant_id=uid,
        bank_statement_id=body.bank_statement_id,
        journal_entry_id=body.journal_entry_id,
    )


@router.post("/statements/{statement_id}/exclude")
async def exclude_statement(
    statement_id: str,
    user: UserContext = Depends(require_permission("bank_recon.write")),
):
    """Exclude a statement line from reconciliation (e.g. bank charges)."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await bank_recon_service.exclude_statement(
        restaurant_id=uid,
        bank_statement_id=statement_id,
    )


# ══════════════════════════════════════════════════════════════════════════════
# QUERIES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/statements")
async def list_statements(
    status: Optional[str] = Query(None, description="Filter: unmatched|matched|excluded"),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    """List bank statement lines with optional filters."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await bank_recon_service.list_statements(
        uid, status, from_date, to_date, limit, offset,
    )


@router.get("/summary")
async def reconciliation_summary(
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    """Reconciliation summary — matched vs unmatched counts + ledger vs bank difference."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await bank_recon_service.reconciliation_summary(uid, from_date, to_date)
