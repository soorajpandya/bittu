"""Accounting Reports endpoints — Daybook, Ledger, Journal Entries,
Trial Balance, P&L Statement, Balance Sheet, GST Reports.

Provides:
  GET  /accounting/reports/daybook           — daily transaction summary
  GET  /accounting/reports/ledger/{id}       — account ledger with running balance
  GET  /accounting/reports/journal-entries    — paginated journal entries list
  GET  /accounting/reports/journal-entries/{id} — single journal detail
  GET  /accounting/reports/trial-balance     — trial balance as-of date
  GET  /accounting/reports/profit-and-loss   — income statement for date range
  GET  /accounting/reports/balance-sheet     — balance sheet as-of date
  GET  /accounting/reports/gst/summary       — GST payable / refundable summary
  GET  /accounting/reports/gst/gstr1         — outward supplies (GSTR-1)
  GET  /accounting/reports/gst/gstr2         — inward supplies (GSTR-2)
  GET  /accounting/reports/gst/hsn-summary   — HSN/SAC wise summary
"""
from typing import Optional
from datetime import date
from fastapi import APIRouter, Depends, Query

from app.core.auth import UserContext, require_permission
from app.services.accounting_reports_service import (
    get_daybook,
    get_ledger,
    get_journal_entries,
    get_journal_detail,
    get_trial_balance,
    get_profit_and_loss,
    get_balance_sheet,
    get_gstr1_report,
    get_gstr2_report,
    get_gst_summary,
    get_gst_hsn_summary,
)

router = APIRouter(prefix="/accounting/reports", tags=["Accounting – Reports"])

_auth = require_permission("accounting:read")


def _uid(user: UserContext) -> str:
    return user.owner_id if user.is_branch_user else user.user_id


# ──────────────────────────────────────────────────────────────
# Daybook
# ──────────────────────────────────────────────────────────────

@router.get("/daybook")
async def daybook(
    target_date: Optional[date] = Query(None, description="Date (defaults to today)"),
    user: UserContext = Depends(_auth),
):
    """All transactions for a single day — invoices, payments, bills, expenses, journals."""
    return await get_daybook(
        target_date=target_date or date.today(),
        user_id=_uid(user),
        branch_id=user.branch_id,
    )


# ──────────────────────────────────────────────────────────────
# Ledger
# ──────────────────────────────────────────────────────────────

@router.get("/ledger/{account_id}")
async def ledger(
    account_id: str,
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(_auth),
):
    """Account ledger with opening balance, line-item movements, and running balance."""
    return await get_ledger(
        account_id=account_id,
        user_id=_uid(user),
        branch_id=user.branch_id,
        from_date=from_date,
        to_date=to_date,
        page=page,
        per_page=per_page,
    )


# ──────────────────────────────────────────────────────────────
# Journal Entries
# ──────────────────────────────────────────────────────────────

@router.get("/journal-entries")
async def journal_entries(
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(_auth),
):
    """List journal entries with their line items."""
    return await get_journal_entries(
        user_id=_uid(user),
        branch_id=user.branch_id,
        from_date=from_date,
        to_date=to_date,
        status=status,
        page=page,
        per_page=per_page,
    )


@router.get("/journal-entries/{journal_id}")
async def journal_entry_detail(
    journal_id: str,
    user: UserContext = Depends(_auth),
):
    """Single journal entry with full line-item detail."""
    return await get_journal_detail(
        journal_id=journal_id,
        user_id=_uid(user),
    )


# ──────────────────────────────────────────────────────────────
# Trial Balance
# ──────────────────────────────────────────────────────────────

@router.get("/trial-balance")
async def trial_balance(
    as_of_date: Optional[date] = Query(None, description="Defaults to today"),
    user: UserContext = Depends(_auth),
):
    """Trial balance — debit/credit totals per account."""
    return await get_trial_balance(
        user_id=_uid(user),
        branch_id=user.branch_id,
        as_of_date=as_of_date,
    )


# ──────────────────────────────────────────────────────────────
# Profit & Loss
# ──────────────────────────────────────────────────────────────

@router.get("/profit-and-loss")
async def profit_and_loss(
    from_date: Optional[date] = Query(None, description="Defaults to FY start (Apr 1)"),
    to_date: Optional[date] = Query(None, description="Defaults to today"),
    user: UserContext = Depends(_auth),
):
    """Profit & Loss (Income Statement) for a date range."""
    return await get_profit_and_loss(
        user_id=_uid(user),
        branch_id=user.branch_id,
        from_date=from_date,
        to_date=to_date,
    )


# ──────────────────────────────────────────────────────────────
# Balance Sheet
# ──────────────────────────────────────────────────────────────

@router.get("/balance-sheet")
async def balance_sheet(
    as_of_date: Optional[date] = Query(None, description="Defaults to today"),
    user: UserContext = Depends(_auth),
):
    """Balance Sheet — Assets = Liabilities + Equity."""
    return await get_balance_sheet(
        user_id=_uid(user),
        branch_id=user.branch_id,
        as_of_date=as_of_date,
    )


# ──────────────────────────────────────────────────────────────
# GST Reports
# ──────────────────────────────────────────────────────────────

@router.get("/gst/summary")
async def gst_summary(
    from_date: Optional[date] = Query(None, description="Defaults to 1st of current month"),
    to_date: Optional[date] = Query(None, description="Defaults to today"),
    user: UserContext = Depends(_auth),
):
    """GST summary — output tax vs input tax → net payable / refundable."""
    return await get_gst_summary(
        user_id=_uid(user),
        branch_id=user.branch_id,
        from_date=from_date,
        to_date=to_date,
    )


@router.get("/gst/gstr1")
async def gstr1(
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    user: UserContext = Depends(_auth),
):
    """GSTR-1: Outward supplies — sales invoices + credit notes with tax breakup."""
    return await get_gstr1_report(
        user_id=_uid(user),
        branch_id=user.branch_id,
        from_date=from_date,
        to_date=to_date,
    )


@router.get("/gst/gstr2")
async def gstr2(
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    user: UserContext = Depends(_auth),
):
    """GSTR-2: Inward supplies — purchase bills + expenses + debit notes."""
    return await get_gstr2_report(
        user_id=_uid(user),
        branch_id=user.branch_id,
        from_date=from_date,
        to_date=to_date,
    )


@router.get("/gst/hsn-summary")
async def gst_hsn_summary(
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    user: UserContext = Depends(_auth),
):
    """HSN/SAC-wise summary for GST returns."""
    return await get_gst_hsn_summary(
        user_id=_uid(user),
        branch_id=user.branch_id,
        from_date=from_date,
        to_date=to_date,
    )
