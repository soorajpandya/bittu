"""
Financial Reports API — Trial Balance, P&L, Balance Sheet, Aging, GST Summary.

Exposes the accounting engine's reporting methods + subledger aging reports
through a unified /reports prefix.
"""
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, Query

from app.core.auth import UserContext, require_permission
from app.core.logging import get_logger
from app.services.accounting_engine import accounting_engine
from app.services.subledger_service import subledger_service
from app.services.tax_service import tax_service
from app.services.expense_service import ExpenseService

router = APIRouter(prefix="/reports", tags=["Financial Reports"])
logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# TRIAL BALANCE
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/trial-balance")
async def trial_balance(
    as_of: Optional[date] = Query(None, description="As-of date (defaults to today)"),
    user: UserContext = Depends(require_permission("reports.read")),
):
    """Trial Balance — debit/credit totals per account. Balanced books = totals match."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await accounting_engine.get_trial_balance(uid, as_of)


# ══════════════════════════════════════════════════════════════════════════════
# BALANCE SHEET
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/balance-sheet")
async def balance_sheet(
    as_of: Optional[date] = Query(None, description="As-of date (defaults to today)"),
    user: UserContext = Depends(require_permission("reports.read")),
):
    """Balance Sheet — Assets = Liabilities + Equity."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await accounting_engine.get_balance_sheet(uid, as_of)


# ══════════════════════════════════════════════════════════════════════════════
# INCOME STATEMENT (P&L)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/income-statement")
async def income_statement(
    from_date: Optional[date] = Query(None, description="Period start"),
    to_date: Optional[date] = Query(None, description="Period end"),
    user: UserContext = Depends(require_permission("reports.read")),
):
    """Income Statement (P&L) — Revenue, Expenses, Net Income for a period."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await accounting_engine.get_income_statement(uid, from_date, to_date)


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOMER AGING (AR)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/customer-aging")
async def customer_aging(
    customer_id: Optional[str] = Query(None),
    as_of: Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("reports.read")),
):
    """Customer Aging Report — 30/60/90+ day AR buckets."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await subledger_service.customer_aging(uid, customer_id, as_of)


# ══════════════════════════════════════════════════════════════════════════════
# SUPPLIER AGING (AP)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/supplier-aging")
async def supplier_aging(
    supplier_id: Optional[str] = Query(None),
    as_of: Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("reports.read")),
):
    """Supplier Aging Report — 30/60/90+ day AP buckets."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await subledger_service.supplier_aging(uid, supplier_id, as_of)


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOMER STATEMENT
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/customer-statement/{customer_id}")
async def customer_statement(
    customer_id: str,
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(200, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("reports.read")),
):
    """Customer Statement — full ledger of a customer's transactions + balance."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    ledger = await subledger_service.get_customer_ledger(
        uid, customer_id, from_date, to_date, limit, offset,
    )
    balance = await subledger_service.get_customer_balance(uid, customer_id)
    return {
        "customer_id": customer_id,
        "balance": balance,
        "entries": ledger,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SUPPLIER STATEMENT
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/supplier-statement/{supplier_id}")
async def supplier_statement(
    supplier_id: str,
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(200, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("reports.read")),
):
    """Supplier Statement — full ledger of a supplier's transactions + balance."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    ledger = await subledger_service.get_supplier_ledger(
        uid, supplier_id, from_date, to_date, limit, offset,
    )
    balance = await subledger_service.get_supplier_balance(uid, supplier_id)
    return {
        "supplier_id": supplier_id,
        "balance": balance,
        "entries": ledger,
    }


# ══════════════════════════════════════════════════════════════════════════════
# EXPENSE SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/expense-summary")
async def expense_summary(
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("reports.read")),
):
    """Expense Summary — breakdown by category for a period."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    svc = ExpenseService()
    return await svc.expense_summary(uid, from_date, to_date)


# ══════════════════════════════════════════════════════════════════════════════
# GST SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/gst-summary")
async def gst_summary(
    period_start: date = Query(..., description="GST period start"),
    period_end: date = Query(..., description="GST period end"),
    user: UserContext = Depends(require_permission("reports.read")),
):
    """
    GST Summary — GSTR-3B style data for a period.
    Returns CGST/SGST/IGST collected, input credits, and net payable.
    """
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await tax_service.gst_return_data(uid, period_start, period_end)


# ══════════════════════════════════════════════════════════════════════════════
# CASH FLOW STATEMENT
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/cash-flow")
async def cash_flow(
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("reports.read")),
):
    """
    Cash Flow summary — inflows and outflows from cash/bank accounts.
    Derived from journal entries touching cash, bank, and card accounts.
    """
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await _compute_cash_flow(uid, from_date, to_date)


async def _compute_cash_flow(
    restaurant_id: str,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> dict:
    """Compute cash flow from journal entries on cash/bank/card accounts."""
    from uuid import UUID
    from app.core.database import get_connection

    rid = UUID(restaurant_id)
    if not from_date:
        from_date = date.today().replace(day=1)
    if not to_date:
        to_date = date.today()

    async with get_connection() as conn:
        rows = await conn.fetch(
            """SELECT
                   coa.system_code,
                   coa.name AS account_name,
                   je.reference_type,
                   COALESCE(SUM(jl.debit), 0) AS total_debit,
                   COALESCE(SUM(jl.credit), 0) AS total_credit
               FROM journal_lines jl
               JOIN journal_entries je ON je.id = jl.journal_entry_id
               JOIN chart_of_accounts coa ON coa.id = jl.account_id
               WHERE je.restaurant_id = $1
                 AND je.entry_date BETWEEN $2 AND $3
                 AND je.is_reversed = false
                 AND coa.system_code IN (
                     'CASH_ACCOUNT', 'UPI_ACCOUNT', 'CARD_ACCOUNT'
                 )
               GROUP BY coa.system_code, coa.name, je.reference_type
               ORDER BY coa.system_code, je.reference_type""",
            rid, from_date, to_date,
        )

    inflows = []
    outflows = []
    total_inflow = 0.0
    total_outflow = 0.0

    for r in rows:
        d = float(r["total_debit"])
        c = float(r["total_credit"])
        net = d - c

        entry = {
            "account": r["account_name"],
            "system_code": r["system_code"],
            "reference_type": r["reference_type"],
            "amount": abs(net),
        }

        if net > 0:
            inflows.append(entry)
            total_inflow += net
        elif net < 0:
            outflows.append(entry)
            total_outflow += abs(net)

    return {
        "period": {"from": from_date.isoformat(), "to": to_date.isoformat()},
        "inflows": inflows,
        "outflows": outflows,
        "total_inflow": round(total_inflow, 2),
        "total_outflow": round(total_outflow, 2),
        "net_cash_flow": round(total_inflow - total_outflow, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT DRILLDOWN
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/drilldown")
async def audit_drilldown(
    account_id: Optional[str] = Query(None, description="Filter by account (from trial balance)"),
    reference_type: Optional[str] = Query(None, description="Filter by source type (order, payment, refund, etc.)"),
    reference_id: Optional[str] = Query(None, description="Filter by specific source document ID"),
    from_date: Optional[date] = Query(None, description="Entry date from"),
    to_date: Optional[date] = Query(None, description="Entry date to"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("reports.read")),
):
    """
    Audit Drilldown — trace any number back to its source journal entries.

    Usage:
      - From trial balance → pass account_id to see all entries for that account
      - From P&L line → pass reference_type to see all entries of that type
      - From specific transaction → pass reference_id to see the exact entry
      - CA asks "show me origin of this number" → this endpoint answers it
    """
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await accounting_engine.drilldown(
        uid,
        account_id=account_id,
        reference_type=reference_type,
        reference_id=reference_id,
        entry_date_from=from_date,
        entry_date_to=to_date,
        limit=limit,
        offset=offset,
    )


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRITY CHECK
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/integrity-check")
async def integrity_check(
    user: UserContext = Depends(require_permission("reports.read")),
):
    """
    Accounting Integrity Validator — run full consistency check.

    Checks:
      1. Trial balance: sum(debit) == sum(credit) globally
      2. Entry balance: every individual journal entry balances
      3. Orphan lines: journal_lines without parent entry
      4. Broken account refs: lines referencing deleted accounts
      5. Reversal integrity: reversed entries properly linked

    Returns all_passed=true if system is fully consistent.
    """
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await accounting_engine.check_integrity(uid)
