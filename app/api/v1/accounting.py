"""Accounting, Chart of Accounts, and Reports endpoints."""
from datetime import date, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.core.database import get_connection
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.services.accounting_service import AccountingService, get_account_by_code
from app.services.accounting_engine import accounting_engine

router = APIRouter(prefix="/accounting", tags=["Accounting"])
accounts_router = APIRouter(prefix="/accounts", tags=["Chart of Accounts"])
reports_router = APIRouter(prefix="/reports", tags=["Reports"])
_svc = AccountingService()
logger = get_logger(__name__)


class ExpenseCreate(BaseModel):
    amount: float
    category: str
    description: str = ""
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None


@router.get("/cash-flow")
async def cash_flow(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("accounting.read")),
):
    """Revenue vs expenses for a period (default: last 30 days)."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    if not start_date:
        start_date = date.today() - timedelta(days=30)
    if not end_date:
        end_date = date.today()
    return await _svc.get_cash_flow(uid, branch_id or user.branch_id, start_date, end_date)


@router.get("/entries")
async def list_entries(
    entry_type: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    branch_id: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("accounting.read")),
):
    """List accounting entries with filters."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await _svc.get_entries(
        uid, branch_id or user.branch_id, entry_type, start_date, end_date, limit, offset
    )


@router.get("/daily-breakdown")
async def daily_breakdown(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("accounting.read")),
):
    """Revenue and expenses grouped by day."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    if not start_date:
        start_date = date.today() - timedelta(days=30)
    if not end_date:
        end_date = date.today()
    return await _svc.get_daily_breakdown(uid, branch_id or user.branch_id, start_date, end_date)


@router.get("/payment-methods")
async def payment_method_breakdown(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("accounting.read")),
):
    """Revenue split by payment method."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    if not start_date:
        start_date = date.today() - timedelta(days=30)
    if not end_date:
        end_date = date.today()
    return await _svc.get_payment_method_breakdown(uid, branch_id or user.branch_id, start_date, end_date)


@router.post("/expenses", status_code=201)
async def record_expense(
    body: ExpenseCreate,
    user: UserContext = Depends(require_permission("accounting.write")),
):
    """Manually record an expense (double-entry via accounting engine)."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")

    import uuid as _uuid

    expense_id = body.reference_id or str(_uuid.uuid4())
    try:
        journal_id = await accounting_engine.record_expense(
            restaurant_id=restaurant_id,
            branch_id=user.branch_id,
            expense_id=expense_id,
            amount=body.amount,
            description=body.description or body.category,
            created_by=uid,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Also write legacy row for backward compat
    legacy_result = await _svc.record_expense(
        user_id=uid,
        restaurant_id=restaurant_id,
        branch_id=user.branch_id,
        amount=body.amount,
        category=body.category,
        description=body.description,
        reference_type=body.reference_type,
        reference_id=body.reference_id,
    )

    return {**legacy_result, "journal_entry_id": journal_id}


# ── Chart of Accounts ──────────────────────────────────────────────────────────

@accounts_router.get("")
async def list_accounts(
    account_type: Optional[str] = Query(None, description="Filter by type: asset|liability|equity|revenue|expense"),
    user: UserContext = Depends(require_permission("accounting.read")),
):
    """List all Chart of Accounts for the restaurant."""
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")

    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, account_code, name, account_type, parent_id,
                   system_code, is_system, is_active, description
              FROM chart_of_accounts
             WHERE restaurant_id = $1
               AND is_active = true
               AND ($2::text IS NULL OR account_type = $2)
             ORDER BY account_code
            """,
            restaurant_id, account_type,
        )
    return [dict(r) for r in rows]


@accounts_router.get("/tree")
async def accounts_tree(
    user: UserContext = Depends(require_permission("accounting.read")),
):
    """Return the Chart of Accounts as a hierarchical tree."""
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")

    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, account_code, name, account_type,
                   parent_id, system_code, is_system, is_active
              FROM chart_of_accounts
             WHERE restaurant_id = $1 AND is_active = true
             ORDER BY account_code
            """,
            restaurant_id,
        )

    accounts = {str(r["id"]): {**dict(r), "id": str(r["id"]),
                                "parent_id": str(r["parent_id"]) if r["parent_id"] else None,
                                "children": []}
                for r in rows}
    roots = []
    for acc in accounts.values():
        pid = acc["parent_id"]
        if pid and pid in accounts:
            accounts[pid]["children"].append(acc)
        else:
            roots.append(acc)
    return roots


@accounts_router.get("/{account_id}/ledger")
async def account_ledger(
    account_id: str,
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("accounting.read")),
):
    """
    T-account ledger for a single CoA account.
    Returns all movements with running balance.
    """
    if not from_date:
        from_date = date.today() - timedelta(days=30)
    if not to_date:
        to_date = date.today()
    try:
        return await _svc.get_ledger(
            account_id=account_id,
            from_date=from_date,
            to_date=to_date,
            restaurant_id=user.restaurant_id,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ── Reports ────────────────────────────────────────────────────────────────────

@reports_router.get("/pnl")
async def profit_and_loss(
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("accounting.read")),
):
    """
    Profit & Loss statement.

    Revenue  = SUM of credit entries on revenue accounts
    Expenses = SUM of debit entries on expense accounts
    Profit   = Revenue − Expenses
    """
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    if not from_date:
        from_date = date.today() - timedelta(days=30)
    if not to_date:
        to_date = date.today()
    try:
        return await _svc.get_pnl(
            restaurant_id=restaurant_id,
            from_date=from_date,
            to_date=to_date,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@reports_router.get("/cashflow")
async def cashflow_report(
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("accounting.read")),
):
    """Cash inflow vs outflow summary (merges legacy + double-entry rows)."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    if not from_date:
        from_date = date.today() - timedelta(days=30)
    if not to_date:
        to_date = date.today()
    return await _svc.get_cash_flow(uid, branch_id or user.branch_id, from_date, to_date)


# ── Trial Balance ──────────────────────────────────────────────────────────────

@reports_router.get("/trial-balance")
async def trial_balance(
    as_of_date: Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("accounting.report")),
):
    """
    Trial Balance — every account's total debits and credits.
    Total debits MUST equal total credits if books are balanced.
    """
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    try:
        return await accounting_engine.get_trial_balance(
            restaurant_id=restaurant_id,
            as_of_date=as_of_date,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── Balance Sheet ──────────────────────────────────────────────────────────────

@reports_router.get("/balance-sheet")
async def balance_sheet(
    as_of_date: Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("accounting.report")),
):
    """
    Balance Sheet — Assets = Liabilities + Equity.
    Includes retained earnings (accumulated profit).
    """
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    try:
        return await accounting_engine.get_balance_sheet(
            restaurant_id=restaurant_id,
            as_of_date=as_of_date,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── Journal Entry Reversal ─────────────────────────────────────────────────────

class ReversalRequest(BaseModel):
    journal_entry_id: str
    reason: str = ""


@router.post("/reversals", status_code=201)
async def reverse_journal_entry(
    body: ReversalRequest,
    user: UserContext = Depends(require_permission("accounting.write")),
):
    """
    Reverse a journal entry. Creates a new entry with swapped debit/credit.
    The original entry is marked as reversed.
    """
    uid = user.owner_id if user.is_branch_user else user.user_id
    try:
        reversal_id = await accounting_engine.reverse_entry(
            journal_entry_id=body.journal_entry_id,
            reason=body.reason,
            created_by=uid,
        )
        return {"reversal_journal_entry_id": reversal_id}
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
