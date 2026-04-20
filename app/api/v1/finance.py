"""
Finance API — Financial Operating System (Product Layer)
────────────────────────────────────────────────────────
/finance/* router — not just APIs, but opinionated product workflows:

  ─── DASHBOARDS & STATUS ─────────────────────────────────────────────
  GET  /finance/dashboard              Real-time 17-metric dashboard
  GET  /finance/trust-status           System health badges (✔️ 🔒 ⚠️)
  GET  /finance/ca-view                Everything a CA needs in one call

  ─── DAILY CLOSING WORKFLOW ──────────────────────────────────────────
  POST /finance/daily-close/init       Compute expected cash from ledger
  POST /finance/daily-close/cash-count Cashier enters actual amounts
  POST /finance/daily-close/close      Manager locks the day
  GET  /finance/daily-close/history    Closing history with filters

  ─── GST FILING WORKFLOW ─────────────────────────────────────────────
  POST /finance/gst/workflow/generate          Generate from ledger
  POST /finance/gst/workflow/{id}/review       Mark reviewed
  POST /finance/gst/workflow/{id}/export       Mark exported
  POST /finance/gst/workflow/{id}/file         Record filing ref
  POST /finance/gst/workflow/{id}/pay          Record payment
  GET  /finance/gst/workflow                   List workflows

  ─── INSIGHT ENGINE ──────────────────────────────────────────────────
  GET  /finance/insights/profit        "Why did profit drop?" with insights
  GET  /finance/insights/channels      Channel revenue/margin breakdown
  GET  /finance/insights/cash-mismatch Cash mismatch pattern analysis

  ─── ACTIONABLE ALERTS ───────────────────────────────────────────────
  GET  /finance/alerts                 List with severity + suggested action
  GET  /finance/alerts/summary         Quick badge counts
  POST /finance/alerts/scan            Run anomaly scanner
  POST /finance/alerts/{id}/resolve    Resolve with notes

  ─── STANDARD FINANCE ────────────────────────────────────────────────
  Reports, Ledger, Invoices, Expenses, Bank Recon, GST,
  Drilldown, Periods, Journals, Integrity, Audit Log, Trend
"""

from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, Query, UploadFile, File

from app.core.auth import UserContext, require_permission
from app.core.logging import get_logger
from app.services.accounting_engine import accounting_engine
from app.services.subledger_service import subledger_service
from app.services.invoice_service import invoice_service
from app.services.expense_service import ExpenseService
from app.services.tax_service import tax_service
from app.services.bank_recon_service import bank_recon_service
from app.services.finance_service import finance_service

router = APIRouter(prefix="/finance", tags=["Financial Operating System"])
logger = get_logger(__name__)


def _rid(user: UserContext) -> str:
    """Resolve restaurant_id (owner_id for branch staff)."""
    return user.owner_id if user.is_branch_user else user.user_id


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/dashboard")
async def dashboard(
    branch_id: Optional[str] = Query(None, description="Filter by branch"),
    user: UserContext = Depends(require_permission("finance.dashboard")),
):
    """
    Real-time financial dashboard — all key metrics in a single call.

    Revenue (today + MTD), cash/bank/card balances, GST payable,
    AR/AP outstanding, COGS %, trial balance status, unreconciled count.
    """
    return await finance_service.get_dashboard(
        _rid(user), branch_id=branch_id or (user.branch_id if user.is_branch_user else None),
    )


# ══════════════════════════════════════════════════════════════════════════════
# REPORTS (with branch filtering)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/reports/trial-balance")
async def trial_balance(
    as_of: Optional[date] = Query(None),
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("finance.report")),
):
    bid = branch_id or (user.branch_id if user.is_branch_user else None)
    return await accounting_engine.get_trial_balance(_rid(user), as_of, branch_id=bid)


@router.get("/reports/balance-sheet")
async def balance_sheet(
    as_of: Optional[date] = Query(None),
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("finance.report")),
):
    bid = branch_id or (user.branch_id if user.is_branch_user else None)
    return await accounting_engine.get_balance_sheet(_rid(user), as_of, branch_id=bid)


@router.get("/reports/income-statement")
async def income_statement(
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("finance.report")),
):
    bid = branch_id or (user.branch_id if user.is_branch_user else None)
    return await accounting_engine.get_income_statement(
        _rid(user), from_date, to_date, branch_id=bid,
    )


@router.get("/reports/cash-flow")
async def cash_flow(
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("finance.report")),
):
    uid = _rid(user)
    return await _compute_cash_flow(uid, from_date, to_date)


# ── Cash flow helper (same logic as reports.py) ─────────────────────────────

async def _compute_cash_flow(restaurant_id: str, from_date=None, to_date=None):
    from uuid import UUID
    from app.core.database import get_connection

    rid = UUID(restaurant_id)
    if not from_date:
        from_date = date.today().replace(day=1)
    if not to_date:
        to_date = date.today()

    async with get_connection() as conn:
        rows = await conn.fetch(
            """SELECT coa.system_code, coa.name AS account_name,
                      je.reference_type,
                      COALESCE(SUM(jl.debit), 0) AS total_debit,
                      COALESCE(SUM(jl.credit), 0) AS total_credit
               FROM journal_lines jl
               JOIN journal_entries je ON je.id = jl.journal_entry_id
               JOIN chart_of_accounts coa ON coa.id = jl.account_id
               WHERE je.restaurant_id = $1
                 AND je.entry_date BETWEEN $2 AND $3
                 AND je.is_reversed = false
                 AND coa.system_code IN ('CASH_ACCOUNT','UPI_ACCOUNT','CARD_ACCOUNT')
               GROUP BY coa.system_code, coa.name, je.reference_type
               ORDER BY coa.system_code, je.reference_type""",
            rid, from_date, to_date,
        )

    inflows, outflows = [], []
    total_in, total_out = 0.0, 0.0
    for r in rows:
        net = float(r["total_debit"]) - float(r["total_credit"])
        entry = {"account": r["account_name"], "system_code": r["system_code"],
                 "reference_type": r["reference_type"], "amount": abs(net)}
        if net > 0:
            inflows.append(entry); total_in += net
        elif net < 0:
            outflows.append(entry); total_out += abs(net)

    return {"period": {"from": from_date.isoformat(), "to": to_date.isoformat()},
            "inflows": inflows, "outflows": outflows,
            "total_inflow": round(total_in, 2), "total_outflow": round(total_out, 2),
            "net_cash_flow": round(total_in - total_out, 2)}


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOMER LEDGER & AGING
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/customers/aging")
async def customer_aging(
    customer_id: Optional[str] = Query(None),
    as_of: Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await subledger_service.customer_aging(_rid(user), customer_id, as_of)


@router.get("/customers/balances")
async def all_customer_balances(
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await subledger_service.all_customer_balances(_rid(user))


@router.get("/customers/{customer_id}/ledger")
async def customer_ledger(
    customer_id: str,
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(200, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("finance.report")),
):
    uid = _rid(user)
    ledger = await subledger_service.get_customer_ledger(uid, customer_id, from_date, to_date, limit, offset)
    balance = await subledger_service.get_customer_balance(uid, customer_id)
    return {"customer_id": customer_id, "balance": balance, "entries": ledger}


# ══════════════════════════════════════════════════════════════════════════════
# SUPPLIER LEDGER & AGING
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/suppliers/aging")
async def supplier_aging(
    supplier_id: Optional[str] = Query(None),
    as_of: Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await subledger_service.supplier_aging(_rid(user), supplier_id, as_of)


@router.get("/suppliers/balances")
async def all_supplier_balances(
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await subledger_service.all_supplier_balances(_rid(user))


@router.get("/suppliers/{supplier_id}/ledger")
async def supplier_ledger(
    supplier_id: str,
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(200, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("finance.report")),
):
    uid = _rid(user)
    ledger = await subledger_service.get_supplier_ledger(uid, supplier_id, from_date, to_date, limit, offset)
    balance = await subledger_service.get_supplier_balance(uid, supplier_id)
    return {"supplier_id": supplier_id, "balance": balance, "entries": ledger}


# ══════════════════════════════════════════════════════════════════════════════
# INVOICES (AR)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/invoices", status_code=201)
async def create_invoice(
    payload: dict,
    user: UserContext = Depends(require_permission("finance.report")),
):
    uid = _rid(user)
    return await invoice_service.create_invoice(
        restaurant_id=uid,
        customer_id=payload["customer_id"],
        items=payload["items"],
        due_date=payload.get("due_date"),
        notes=payload.get("notes"),
        created_by=user.user_id,
    )


@router.get("/invoices")
async def list_invoices(
    status: Optional[str] = Query(None),
    customer_id: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await invoice_service.list_invoices(
        _rid(user), status=status, customer_id=customer_id, limit=limit, offset=offset,
    )


@router.get("/invoices/unpaid")
async def unpaid_invoices(
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await invoice_service.list_invoices(_rid(user), status="unpaid")


@router.get("/invoices/{invoice_id}")
async def get_invoice(
    invoice_id: str,
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await invoice_service.get_invoice(_rid(user), invoice_id)


@router.post("/invoices/{invoice_id}/payment")
async def record_invoice_payment(
    invoice_id: str,
    payload: dict,
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await invoice_service.record_invoice_payment(
        _rid(user), invoice_id,
        amount=payload["amount"],
        payment_method=payload.get("payment_method", "cash"),
        reference=payload.get("reference"),
    )


@router.post("/invoices/{invoice_id}/void")
async def void_invoice(
    invoice_id: str,
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await invoice_service.void_invoice(_rid(user), invoice_id, user.user_id)


# ══════════════════════════════════════════════════════════════════════════════
# EXPENSES
# ══════════════════════════════════════════════════════════════════════════════

_expense_svc = ExpenseService()


@router.post("/expenses", status_code=201)
async def create_expense(
    payload: dict,
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await _expense_svc.create_expense(
        restaurant_id=_rid(user),
        category_id=payload["category_id"],
        amount=payload["amount"],
        description=payload.get("description", ""),
        payment_method=payload.get("payment_method", "cash"),
        receipt_url=payload.get("receipt_url"),
        created_by=user.user_id,
        vendor_name=payload.get("vendor_name"),
        expense_date=payload.get("expense_date"),
    )


@router.get("/expenses")
async def list_expenses(
    category_id: Optional[str] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await _expense_svc.list_expenses(
        _rid(user), category_id=category_id, from_date=from_date,
        to_date=to_date, status=status, limit=limit, offset=offset,
    )


@router.get("/expenses/summary")
async def expense_summary(
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await _expense_svc.expense_summary(_rid(user), from_date, to_date)


@router.get("/expenses/categories")
async def list_expense_categories(
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await _expense_svc.list_categories(_rid(user))


@router.post("/expenses/categories", status_code=201)
async def create_expense_category(
    payload: dict,
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await _expense_svc.create_category(
        _rid(user), name=payload["name"], description=payload.get("description"),
    )


@router.get("/expenses/{expense_id}")
async def get_expense(
    expense_id: str,
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await _expense_svc.get_expense(_rid(user), expense_id)


@router.post("/expenses/{expense_id}/approve")
async def approve_expense(
    expense_id: str,
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await _expense_svc.approve_expense(_rid(user), expense_id, user.user_id)


# ══════════════════════════════════════════════════════════════════════════════
# BANK RECONCILIATION
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/reconciliation/summary")
async def recon_summary(
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await bank_recon_service.reconciliation_summary(_rid(user))


@router.get("/reconciliation/statements")
async def recon_statements(
    reconciled: Optional[bool] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await bank_recon_service.list_statements(
        _rid(user), reconciled=reconciled, limit=limit, offset=offset,
    )


@router.post("/reconciliation/import-csv")
async def recon_import_csv(
    file: UploadFile = File(...),
    bank_name: str = Query("unknown"),
    user: UserContext = Depends(require_permission("bank_recon.write")),
):
    content = (await file.read()).decode("utf-8")
    return await bank_recon_service.import_statements_csv(_rid(user), content, bank_name)


@router.post("/reconciliation/auto-match")
async def recon_auto_match(
    user: UserContext = Depends(require_permission("bank_recon.write")),
):
    return await bank_recon_service.auto_match(_rid(user))


@router.post("/reconciliation/match")
async def recon_manual_match(
    payload: dict,
    user: UserContext = Depends(require_permission("bank_recon.write")),
):
    return await bank_recon_service.manual_match(
        _rid(user), payload["statement_id"], payload["journal_entry_id"],
    )


@router.post("/reconciliation/unmatch")
async def recon_unmatch(
    payload: dict,
    user: UserContext = Depends(require_permission("bank_recon.write")),
):
    return await bank_recon_service.unmatch(_rid(user), payload["statement_id"])


# ══════════════════════════════════════════════════════════════════════════════
# GST / TAX
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/gst/summary")
async def gst_summary(
    period_start: date = Query(...),
    period_end: date = Query(...),
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await tax_service.gst_return_data(_rid(user), period_start, period_end)


@router.get("/gst/liabilities")
async def gst_liabilities(
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await tax_service.list_liabilities(_rid(user), limit=limit, offset=offset)


@router.post("/gst/compute")
async def gst_compute(
    payload: dict,
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await tax_service.compute_liability(
        _rid(user), payload["period_start"], payload["period_end"],
    )


@router.post("/gst/file")
async def gst_file(
    payload: dict,
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await tax_service.mark_filed(_rid(user), payload["liability_id"])


@router.post("/gst/pay")
async def gst_pay(
    payload: dict,
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await tax_service.record_tax_payment(
        _rid(user), payload["liability_id"],
        payload["amount"], payload.get("payment_method", "bank"),
    )


# ══════════════════════════════════════════════════════════════════════════════
# DRILLDOWN
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/drilldown")
async def drilldown(
    account_id: Optional[str] = Query(None),
    reference_type: Optional[str] = Query(None),
    reference_id: Optional[str] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("finance.audit")),
):
    """Drill into any number back to its source journal entries."""
    return await accounting_engine.drilldown(
        _rid(user), account_id=account_id, reference_type=reference_type,
        reference_id=reference_id, entry_date_from=from_date,
        entry_date_to=to_date, limit=limit, offset=offset,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PERIODS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/periods")
async def list_periods(
    user: UserContext = Depends(require_permission("finance.report")),
):
    return await accounting_engine.list_periods(_rid(user))


@router.post("/periods/close")
async def close_period(
    payload: dict,
    user: UserContext = Depends(require_permission("finance.audit")),
):
    result = await accounting_engine.close_period(
        _rid(user), payload["year"], payload["month"], user.user_id,
    )
    # Log to financial audit trail
    await finance_service.log_financial_action(
        _rid(user), user.user_id, "period.close", "accounting_period",
        f"{payload['year']}-{payload['month']:02d}",
        new_value={"year": payload["year"], "month": payload["month"], "status": "closed"},
    )
    return result


@router.post("/periods/reopen")
async def reopen_period(
    payload: dict,
    user: UserContext = Depends(require_permission("finance.audit")),
):
    result = await accounting_engine.reopen_period(
        _rid(user), payload["year"], payload["month"], user.user_id,
    )
    await finance_service.log_financial_action(
        _rid(user), user.user_id, "period.reopen", "accounting_period",
        f"{payload['year']}-{payload['month']:02d}",
        old_value={"status": "closed"}, new_value={"status": "open"},
    )
    return result


# ══════════════════════════════════════════════════════════════════════════════
# JOURNALS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/journals")
async def search_journals(
    reference_type: Optional[str] = Query(None),
    reference_id: Optional[str] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("finance.audit")),
):
    return await accounting_engine.search_journals(
        _rid(user), reference_type=reference_type, reference_id=reference_id,
        from_date=from_date, to_date=to_date, limit=limit, offset=offset,
    )


@router.post("/journals/reverse", status_code=201)
async def reverse_journal(
    payload: dict,
    user: UserContext = Depends(require_permission("finance.audit")),
):
    result = await accounting_engine.reverse_entry(
        _rid(user), payload["journal_entry_id"], user.user_id,
        payload.get("reason", "Manual reversal"),
    )
    await finance_service.log_financial_action(
        _rid(user), user.user_id, "journal.reverse", "journal_entry",
        payload["journal_entry_id"],
        new_value={"reason": payload.get("reason", "Manual reversal")},
    )
    return result


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRITY CHECK
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/integrity-check")
async def integrity_check(
    user: UserContext = Depends(require_permission("finance.audit")),
):
    return await accounting_engine.check_integrity(_rid(user))


# ══════════════════════════════════════════════════════════════════════════════
# ALERTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/alerts")
async def list_alerts(
    resolved: Optional[bool] = Query(None),
    alert_type: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("finance.dashboard")),
):
    return await finance_service.list_alerts(
        _rid(user), resolved=resolved, alert_type=alert_type,
        severity=severity, limit=limit, offset=offset,
    )


@router.post("/alerts/scan")
async def scan_alerts(
    user: UserContext = Depends(require_permission("finance.audit")),
):
    """Run on-demand financial anomaly scan."""
    count = await finance_service.scan_alerts(_rid(user))
    return {"new_alerts": count}


@router.post("/alerts/{alert_id}/resolve")
async def resolve_alert(
    alert_id: str,
    payload: dict = {},
    user: UserContext = Depends(require_permission("finance.audit")),
):
    notes = payload.get("resolution_notes", "")
    if notes:
        ok = await finance_service.resolve_alert_with_notes(
            _rid(user), alert_id, user.user_id, notes,
        )
    else:
        ok = await finance_service.resolve_alert(_rid(user), alert_id, user.user_id)
    return {"resolved": ok}


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT LOG (with old/new values)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/audit-log")
async def audit_log(
    entity_type: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("finance.audit")),
):
    return await finance_service.list_audit_log(
        _rid(user), entity_type=entity_type, action=action,
        limit=limit, offset=offset,
    )


# ══════════════════════════════════════════════════════════════════════════════
# DAILY REVENUE TREND (for charts)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/trend/revenue")
async def revenue_trend(
    from_date: date = Query(...),
    to_date: date = Query(...),
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("finance.dashboard")),
):
    bid = branch_id or (user.branch_id if user.is_branch_user else None)
    return await finance_service.daily_revenue_trend(_rid(user), from_date, to_date, bid)


# ══════════════════════════════════════════════════════════════════════════════
# MATERIALIZED VIEWS MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/views/refresh")
async def refresh_views(
    user: UserContext = Depends(require_permission("finance.audit")),
):
    """Refresh materialized views (dashboard caches)."""
    await finance_service.refresh_views()
    return {"status": "refreshed"}


# ══════════════════════════════════════════════════════════════════════════════
# DAILY CLOSING WORKFLOW
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/daily-close/init")
async def init_daily_closing(
    payload: dict,
    user: UserContext = Depends(require_permission("finance.daily_close")),
):
    """
    Step 1: Initialize daily closing — system computes expected cash/card/UPI
    from the ledger for the given date.
    """
    closing_date = date.fromisoformat(payload["date"])
    bid = payload.get("branch_id") or (user.branch_id if user.is_branch_user else None)
    result = await finance_service.init_daily_closing(_rid(user), closing_date, bid)
    return result


@router.post("/daily-close/cash-count")
async def submit_cash_count(
    payload: dict,
    user: UserContext = Depends(require_permission("finance.daily_close")),
):
    """
    Step 2: Cashier enters actual counted amounts.
    Returns difference and mismatch flag.
    """
    result = await finance_service.submit_cash_count(
        _rid(user),
        payload["closing_id"],
        user.user_id,
        payload["actual_cash"],
        payload["actual_card"],
        payload["actual_upi"],
        payload.get("notes"),
    )
    # Audit log
    await finance_service.log_financial_action(
        _rid(user), user.user_id, "daily_close.cash_count", "daily_closing",
        payload["closing_id"],
        new_value={
            "actual_cash": payload["actual_cash"],
            "actual_card": payload["actual_card"],
            "actual_upi": payload["actual_upi"],
        },
    )
    return result


@router.post("/daily-close/close")
async def close_day(
    payload: dict,
    user: UserContext = Depends(require_permission("finance.daily_close")),
):
    """
    Step 3: Manager closes the day — locks it, produces summary.
    """
    result = await finance_service.close_day(
        _rid(user), payload["closing_id"], user.user_id,
    )
    await finance_service.log_financial_action(
        _rid(user), user.user_id, "daily_close.close", "daily_closing",
        payload["closing_id"],
        new_value={"status": "closed"},
    )
    return result


@router.get("/daily-close/history")
async def list_daily_closings(
    branch_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(30, le=100),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("finance.daily_close")),
):
    """List daily closings — filterable by branch and status."""
    bid = branch_id or (user.branch_id if user.is_branch_user else None)
    return await finance_service.list_closings(
        _rid(user), branch_id=bid, status=status, limit=limit, offset=offset,
    )


# ══════════════════════════════════════════════════════════════════════════════
# GST FILING WORKFLOW — pipeline: generate → review → export → file → pay
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/gst/workflow/generate")
async def gst_workflow_generate(
    payload: dict,
    user: UserContext = Depends(require_permission("finance.gst")),
):
    """Step 1: Generate GST data for a period from ledger."""
    return await finance_service.gst_workflow_generate(
        _rid(user),
        date.fromisoformat(payload["period_start"]),
        date.fromisoformat(payload["period_end"]),
    )


@router.post("/gst/workflow/{workflow_id}/review")
async def gst_workflow_review(
    workflow_id: str,
    user: UserContext = Depends(require_permission("finance.gst")),
):
    """Step 2: Mark GST data as reviewed."""
    result = await finance_service.gst_workflow_review(
        _rid(user), workflow_id, user.user_id,
    )
    await finance_service.log_financial_action(
        _rid(user), user.user_id, "gst_workflow.review", "gst_filing",
        workflow_id,
    )
    return result


@router.post("/gst/workflow/{workflow_id}/export")
async def gst_workflow_export(
    workflow_id: str,
    user: UserContext = Depends(require_permission("finance.gst")),
):
    """Step 3: Mark as exported for portal filing."""
    return await finance_service.gst_workflow_export(_rid(user), workflow_id)


@router.post("/gst/workflow/{workflow_id}/file")
async def gst_workflow_file(
    workflow_id: str,
    payload: dict,
    user: UserContext = Depends(require_permission("finance.gst")),
):
    """Step 4: Record filing reference from GST portal."""
    result = await finance_service.gst_workflow_file(
        _rid(user), workflow_id, payload["filed_reference"],
    )
    await finance_service.log_financial_action(
        _rid(user), user.user_id, "gst_workflow.file", "gst_filing",
        workflow_id, new_value={"filed_reference": payload["filed_reference"]},
    )
    return result


@router.post("/gst/workflow/{workflow_id}/pay")
async def gst_workflow_pay(
    workflow_id: str,
    payload: dict,
    user: UserContext = Depends(require_permission("finance.gst")),
):
    """Step 5: Record GST payment."""
    result = await finance_service.gst_workflow_pay(
        _rid(user), workflow_id,
        payload["paid_amount"], payload["paid_reference"],
    )
    await finance_service.log_financial_action(
        _rid(user), user.user_id, "gst_workflow.pay", "gst_filing",
        workflow_id,
        new_value={"amount": payload["paid_amount"], "reference": payload["paid_reference"]},
    )
    return result


@router.get("/gst/workflow")
async def list_gst_workflows(
    status: Optional[str] = Query(None),
    limit: int = Query(20, le=100),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("finance.gst")),
):
    """List all GST filing workflows."""
    return await finance_service.list_gst_workflows(
        _rid(user), status=status, limit=limit, offset=offset,
    )


# ══════════════════════════════════════════════════════════════════════════════
# INSIGHT ENGINE — "WHY" answers
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/insights/profit")
async def profit_insight(
    target_date: date = Query(..., description="Date to analyze"),
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("finance.pnl")),
):
    """
    "Why did profit drop?" — compares target date vs same day last week.
    Breaks down revenue, COGS, expenses by channel with natural-language insights.
    """
    bid = branch_id or (user.branch_id if user.is_branch_user else None)
    return await finance_service.profit_insight(_rid(user), target_date, bid)


@router.get("/insights/channels")
async def channel_analysis(
    from_date: date = Query(...),
    to_date: date = Query(...),
    branch_id: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("finance.pnl")),
):
    """Which channel is making/losing money? Revenue, COGS, margin by channel."""
    bid = branch_id or (user.branch_id if user.is_branch_user else None)
    return await finance_service.channel_analysis(_rid(user), from_date, to_date, bid)


@router.get("/insights/cash-mismatch")
async def cash_mismatch_history(
    branch_id: Optional[str] = Query(None),
    days: int = Query(30, le=90),
    user: UserContext = Depends(require_permission("finance.cash")),
):
    """Track cash mismatch pattern over time. Identifies systematic issues."""
    bid = branch_id or (user.branch_id if user.is_branch_user else None)
    return await finance_service.cash_mismatch_history(
        _rid(user), branch_id=bid, days=days,
    )


# ══════════════════════════════════════════════════════════════════════════════
# TRUST STATUS — single call for frontend badges
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/trust-status")
async def trust_status(
    user: UserContext = Depends(require_permission("finance.trust_status")),
):
    """
    Single call returning system health for frontend badges:
    ledger balanced ✔️, period locked 🔒, audit safe, alert count.
    """
    return await finance_service.trust_status(_rid(user))


# ══════════════════════════════════════════════════════════════════════════════
# ALERT SUMMARY (quick badge data)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/alerts/summary")
async def alert_summary(
    user: UserContext = Depends(require_permission("finance.dashboard")),
):
    """Quick alert overview: error/warning/info counts for badges."""
    return await finance_service.alert_summary(_rid(user))


# ══════════════════════════════════════════════════════════════════════════════
# CA VIEW — everything a chartered accountant needs in one call
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/ca-view")
async def ca_view(
    period_start: date = Query(...),
    period_end: date = Query(...),
    user: UserContext = Depends(require_permission("finance.report")),
):
    """
    Single call for CAs: trial balance, income statement, GST summary,
    period lock status, journal entry counts, reversal rate.
    """
    return await finance_service.ca_view(_rid(user), period_start, period_end)
