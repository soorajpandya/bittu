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
accounts_router = APIRouter(prefix="/accounts", tags=["Accounting"])
reports_router = APIRouter(prefix="/reports", tags=["Accounting"])
_svc = AccountingService()
logger = get_logger(__name__)


class ExpenseCreate(BaseModel):
    amount: float
    category: str
    description: str = ""
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None


class IncomeCreate(BaseModel):
    amount: float
    category: str = "other_income"
    description: str = ""
    payment_method: str = "cash"  # cash | upi | card | bank
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

    # Auto-seed Chart of Accounts on first use so a fresh restaurant
    # never gets a 400 from `_resolve_account` because CASH / COGS_FOOD
    # weren't seeded by onboarding.
    async with get_connection() as conn:
        has_coa = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM chart_of_accounts "
            "WHERE restaurant_id = $1::uuid AND is_active = true)",
            restaurant_id,
        )
        if not has_coa:
            await conn.execute("SELECT fn_seed_chart_of_accounts($1::uuid)", restaurant_id)

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


# Map of payment method -> CASH/UPI/CARD account code used by the engine.
_INCOME_RECEIPT_ACCOUNT = {
    "cash": "CASH",
    "upi":  "BANK",
    "bank": "BANK",
    "card": "CARD",
}


@router.post("/income", status_code=201)
async def record_income(
    body: IncomeCreate,
    user: UserContext = Depends(require_permission("accounting.write")),
):
    """
    Manually record non-order income (e.g. catering payment, vendor rebate,
    owner top-up). Mirrors `record_expense`: writes a double-entry journal
    (DR Cash/UPI/Card, CR FOOD_SALES) **and** a legacy `accounting_entries`
    row with `entry_type='revenue'` so it shows up on Daybook + cashflow.
    """
    uid = user.owner_id if user.is_branch_user else user.user_id
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")

    import uuid as _uuid

    # Auto-seed Chart of Accounts on first use (same pattern as /expenses).
    async with get_connection() as conn:
        has_coa = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM chart_of_accounts "
            "WHERE restaurant_id = $1::uuid AND is_active = true)",
            restaurant_id,
        )
        if not has_coa:
            await conn.execute("SELECT fn_seed_chart_of_accounts($1::uuid)", restaurant_id)

    receipt_account = _INCOME_RECEIPT_ACCOUNT.get(
        (body.payment_method or "cash").lower(), "CASH"
    )
    income_id = body.reference_id or str(_uuid.uuid4())
    try:
        journal_id = await accounting_engine.record_income(
            restaurant_id=restaurant_id,
            branch_id=user.branch_id,
            income_id=income_id,
            amount=body.amount,
            receipt_account=receipt_account,
            description=body.description or body.category,
            created_by=uid,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Legacy row for the Daybook drill-down.
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO accounting_entries
                (user_id, restaurant_id, branch_id, entry_type, amount,
                 payment_method, category, reference_type, reference_id, description)
            VALUES ($1, $2, $3, 'revenue', $4, $5, $6, $7, $8, $9)
            RETURNING *
            """,
            uid, restaurant_id, user.branch_id, float(body.amount),
            body.payment_method or "cash", body.category,
            body.reference_type or "income", body.reference_id,
            body.description or body.category,
        )
    return {**dict(row), "journal_entry_id": journal_id}


# ── Edit / Delete ──────────────────────────────────────────────────────────────
#
# Posted journal entries are immutable (DB-enforced) — so "edit" = post a
# reversing journal + mark the legacy bridge row as voided, then create a new
# entry with the new values. "Delete" = same void+reverse, no recreate.

class ExpenseUpdate(BaseModel):
    amount: float
    category: str
    description: str = ""
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None


class IncomeUpdate(BaseModel):
    amount: float
    category: str = "other_income"
    description: str = ""
    payment_method: str = "cash"
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None


class DeleteRequest(BaseModel):
    reason: str = ""


async def _load_owned_entry(conn, entry_id: str, owner_user_id: str) -> dict:
    """Fetch a non-voided accounting_entries row, asserting ownership."""
    try:
        row = await conn.fetchrow(
            "SELECT * FROM accounting_entries WHERE id = $1::uuid FOR UPDATE",
            entry_id,
        )
    except Exception:
        raise HTTPException(status_code=400, detail="invalid entry_id")
    if not row:
        raise HTTPException(status_code=404, detail="entry not found")
    if row["user_id"] != owner_user_id:
        raise HTTPException(status_code=403, detail="not your entry")
    if row["voided_at"] is not None:
        raise HTTPException(status_code=409, detail="entry already voided")
    return dict(row)


async def _void_entry(conn, entry: dict, *, voided_by: str, reason: str) -> Optional[str]:
    """Post a reversing journal (if entry has one) and mark the bridge row voided."""
    reversal_id: Optional[str] = None
    journal_id = entry.get("journal_entry_id")
    if journal_id:
        try:
            reversal_id = await accounting_engine.reverse_entry(
                journal_entry_id=str(journal_id),
                reason=reason or "manual edit/delete",
                created_by=voided_by,
            )
        except ValidationError as exc:
            # already-reversed or period-locked → surface as 409
            raise HTTPException(status_code=409, detail=str(exc))
    await conn.execute(
        """
        UPDATE accounting_entries
           SET voided_at = NOW(),
               voided_by = $2,
               void_reason = $3,
               reversal_journal_id = $4::uuid
         WHERE id = $1::uuid
        """,
        entry["id"], voided_by, reason or None, reversal_id,
    )
    return reversal_id


@router.delete("/entries/{entry_id}", status_code=200)
async def delete_entry(
    entry_id: str,
    body: Optional[DeleteRequest] = None,
    user: UserContext = Depends(require_permission("accounting.write")),
):
    """Soft-delete an accounting entry by posting a reversing journal."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    reason = (body.reason if body else "") or ""
    async with get_connection() as conn:
        async with conn.transaction():
            entry = await _load_owned_entry(conn, entry_id, uid)
            reversal_id = await _void_entry(
                conn, entry, voided_by=uid, reason=reason,
            )
    return {
        "id": str(entry["id"]),
        "voided": True,
        "reversal_journal_id": reversal_id,
    }


@router.put("/expenses/{entry_id}", status_code=200)
async def update_expense(
    entry_id: str,
    body: ExpenseUpdate,
    user: UserContext = Depends(require_permission("accounting.write")),
):
    """Edit a manual expense: void + reverse the original, then create a new entry."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    import uuid as _uuid

    async with get_connection() as conn:
        async with conn.transaction():
            old = await _load_owned_entry(conn, entry_id, uid)
            if old["entry_type"] != "expense":
                raise HTTPException(status_code=400, detail="entry is not an expense")
            await _void_entry(
                conn, old, voided_by=uid, reason=f"edit → new amount {body.amount}",
            )

    # Create the replacement entry (engine + legacy row), same code path as POST.
    new_expense_id = body.reference_id or str(_uuid.uuid4())
    try:
        journal_id = await accounting_engine.record_expense(
            restaurant_id=restaurant_id,
            branch_id=user.branch_id,
            expense_id=new_expense_id,
            amount=body.amount,
            description=body.description or body.category,
            created_by=uid,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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
    return {
        **legacy_result,
        "journal_entry_id": journal_id,
        "replaced_entry_id": str(old["id"]),
    }


@router.put("/income/{entry_id}", status_code=200)
async def update_income(
    entry_id: str,
    body: IncomeUpdate,
    user: UserContext = Depends(require_permission("accounting.write")),
):
    """Edit a manual income entry: void + reverse the original, then create a new entry."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    import uuid as _uuid

    async with get_connection() as conn:
        async with conn.transaction():
            old = await _load_owned_entry(conn, entry_id, uid)
            if old["entry_type"] != "revenue":
                raise HTTPException(status_code=400, detail="entry is not income")
            await _void_entry(
                conn, old, voided_by=uid, reason=f"edit → new amount {body.amount}",
            )

    receipt_account = _INCOME_RECEIPT_ACCOUNT.get(
        (body.payment_method or "cash").lower(), "CASH"
    )
    new_income_id = body.reference_id or str(_uuid.uuid4())
    try:
        journal_id = await accounting_engine.record_income(
            restaurant_id=restaurant_id,
            branch_id=user.branch_id,
            income_id=new_income_id,
            amount=body.amount,
            receipt_account=receipt_account,
            description=body.description or body.category,
            created_by=uid,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO accounting_entries
                (user_id, restaurant_id, branch_id, entry_type, amount,
                 payment_method, category, reference_type, reference_id, description)
            VALUES ($1, $2, $3, 'revenue', $4, $5, $6, $7, $8, $9)
            RETURNING *
            """,
            uid, restaurant_id, user.branch_id, float(body.amount),
            body.payment_method or "cash", body.category,
            body.reference_type or "income", body.reference_id,
            body.description or body.category,
        )
    return {
        **dict(row),
        "journal_entry_id": journal_id,
        "replaced_entry_id": str(old["id"]),
    }


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
    Profit & Loss (Income Statement) — derived ONLY from journal_lines.

    Revenue  = net credit on revenue accounts
    Expenses = net debit on expense accounts
    Net Income = Revenue − Expenses
    """
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    if not from_date:
        from_date = date.today() - timedelta(days=30)
    if not to_date:
        to_date = date.today()
    try:
        return await accounting_engine.get_income_statement(
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


# ── Journal Search ─────────────────────────────────────────────────────────────

@router.get("/journals")
async def search_journals(
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    reference_type: Optional[str] = Query(None),
    reference_id: Optional[str] = Query(None),
    include_reversed: bool = Query(False),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("accounting.read")),
):
    """
    Search journal entries with filters. Returns entries with their lines.
    Every journal entry is traceable to its source event, user, and API.
    """
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    try:
        return await accounting_engine.search_journals(
            restaurant_id=restaurant_id,
            from_date=from_date,
            to_date=to_date,
            reference_type=reference_type,
            reference_id=reference_id,
            include_reversed=include_reversed,
            limit=limit,
            offset=offset,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── Accounting Period Management ───────────────────────────────────────────────

class PeriodCloseRequest(BaseModel):
    period_start: date
    period_end: date
    notes: str = ""


@router.post("/periods/close", status_code=200)
async def close_period(
    body: PeriodCloseRequest,
    user: UserContext = Depends(require_permission("accounting.close_period")),
):
    """
    Close an accounting period. No new journal entries can be created
    for dates within this period after closing.
    """
    uid = user.owner_id if user.is_branch_user else user.user_id
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    if body.period_end < body.period_start:
        raise HTTPException(status_code=400, detail="period_end must be >= period_start")
    try:
        return await accounting_engine.close_period(
            restaurant_id=restaurant_id,
            period_start=body.period_start,
            period_end=body.period_end,
            closed_by=uid,
            notes=body.notes,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/periods/reopen", status_code=200)
async def reopen_period(
    body: PeriodCloseRequest,
    user: UserContext = Depends(require_permission("accounting.reopen_period")),
):
    """
    Reopen a previously closed period. Locked periods cannot be reopened.
    """
    uid = user.owner_id if user.is_branch_user else user.user_id
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    try:
        return await accounting_engine.reopen_period(
            restaurant_id=restaurant_id,
            period_start=body.period_start,
            period_end=body.period_end,
            reopened_by=uid,
            notes=body.notes,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/periods")
async def list_periods(
    user: UserContext = Depends(require_permission("accounting.read")),
):
    """List all accounting periods (open, closed, locked)."""
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    return await accounting_engine.list_periods(restaurant_id=restaurant_id)
