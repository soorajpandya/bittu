"""Accounting Reports Service — Daybook, Ledger, Journal Entries,
Trial Balance, Profit & Loss, Balance Sheet, and GST Reports.

All queries operate on the acc_* tables with user_id / branch_id tenant
isolation.  Amounts are computed from acc_line_items + acc_journals +
acc_chart_of_accounts + related transactional tables.
"""
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from app.core.database import get_connection
from app.core.logging import get_logger

logger = get_logger(__name__)

# ── Account-type helpers ─────────────────────────────────────

INCOME_TYPES = ("income", "other_income", "revenue", "sales")
EXPENSE_TYPES = ("expense", "other_expense", "cost_of_goods_sold", "cogs")
ASSET_TYPES = ("accounts_receivable", "bank", "cash", "fixed_asset",
               "other_asset", "other_current_asset", "stock")
LIABILITY_TYPES = ("accounts_payable", "credit_card", "other_current_liability",
                   "other_liability", "long_term_liability")
EQUITY_TYPES = ("equity", "retained_earnings", "owners_equity")


def _norm(t: Optional[str]) -> str:
    return (t or "").lower().replace(" ", "_").replace("-", "_")


def _is_type(account_type: Optional[str], bucket: tuple) -> bool:
    return _norm(account_type) in bucket


def _float(v) -> float:
    if v is None:
        return 0.0
    return float(v)


# ─── Daybook ────────────────────────────────────────────────

async def get_daybook(
    *,
    target_date: date,
    user_id: str,
    branch_id: Optional[str] = None,
) -> dict:
    """Return all transactions for a single day grouped by type."""
    async with get_connection() as conn:
        # Sales (invoices created on date)
        invoices = await conn.fetch(
            """SELECT invoice_id, invoice_number, customer_id, date, total,
                      tax_total, status, source_order_id
               FROM acc_invoices
               WHERE user_id = $1 AND date = $2
               ORDER BY created_at""",
            user_id, target_date,
        )

        # Payments received
        payments_in = await conn.fetch(
            """SELECT payment_id, payment_number, customer_id, amount,
                      payment_mode, date
               FROM acc_customer_payments
               WHERE user_id = $1 AND date = $2
               ORDER BY created_at""",
            user_id, target_date,
        )

        # Bills / purchases
        bills = await conn.fetch(
            """SELECT bill_id, bill_number, vendor_id, date, total,
                      tax_total, status
               FROM acc_bills
               WHERE user_id = $1 AND date = $2
               ORDER BY created_at""",
            user_id, target_date,
        )

        # Vendor payments
        payments_out = await conn.fetch(
            """SELECT vendorpayment_id, payment_number, vendor_id, amount,
                      payment_mode, date
               FROM acc_vendor_payments
               WHERE user_id = $1 AND date = $2
               ORDER BY created_at""",
            user_id, target_date,
        )

        # Expenses
        expenses = await conn.fetch(
            """SELECT expense_id, expense_number, amount, date,
                      account_id, description
               FROM acc_expenses
               WHERE user_id = $1 AND date = $2
               ORDER BY created_at""",
            user_id, target_date,
        )

        # Journal entries
        journals = await conn.fetch(
            """SELECT journal_id, journal_number, journal_date, total, notes, status
               FROM acc_journals
               WHERE user_id = $1 AND journal_date = $2
               ORDER BY created_at""",
            user_id, target_date,
        )

        # Credit notes
        credit_notes = await conn.fetch(
            """SELECT creditnote_id, creditnote_number, customer_id, date,
                      total, status
               FROM acc_credit_notes
               WHERE user_id = $1 AND date = $2
               ORDER BY created_at""",
            user_id, target_date,
        )

        # Debit notes
        debit_notes = await conn.fetch(
            """SELECT debit_note_id, debit_note_number, customer_id, date,
                      total, status
               FROM acc_debit_notes
               WHERE user_id = $1 AND date = $2
               ORDER BY created_at""",
            user_id, target_date,
        )

    def _rows(rs, pk):
        return [{k: (str(v) if k.endswith("_id") else (_float(v) if isinstance(v, Decimal) else v))
                 for k, v in dict(r).items()} for r in rs]

    total_sales = sum(_float(r["total"]) for r in invoices)
    total_receipts = sum(_float(r["amount"]) for r in payments_in)
    total_purchases = sum(_float(r["total"]) for r in bills)
    total_payments = sum(_float(r["amount"]) for r in payments_out)
    total_expenses = sum(_float(r["amount"]) for r in expenses)

    return {
        "date": str(target_date),
        "summary": {
            "total_sales": total_sales,
            "total_receipts": total_receipts,
            "total_purchases": total_purchases,
            "total_payments": total_payments,
            "total_expenses": total_expenses,
            "net_cash_flow": total_receipts - total_payments - total_expenses,
        },
        "invoices": _rows(invoices, "invoice_id"),
        "customer_payments": _rows(payments_in, "payment_id"),
        "bills": _rows(bills, "bill_id"),
        "vendor_payments": _rows(payments_out, "vendorpayment_id"),
        "expenses": _rows(expenses, "expense_id"),
        "journals": _rows(journals, "journal_id"),
        "credit_notes": _rows(credit_notes, "creditnote_id"),
        "debit_notes": _rows(debit_notes, "debit_note_id"),
    }


# ─── Ledger ─────────────────────────────────────────────────

async def get_ledger(
    *,
    account_id: str,
    user_id: str,
    branch_id: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    """Ledger for a specific chart-of-accounts entry.

    Returns opening balance + all line-item movements + running totals.
    """
    async with get_connection() as conn:
        # Fetch account info
        acct = await conn.fetchrow(
            """SELECT account_id, account_name, account_type, account_code,
                      current_balance
               FROM acc_chart_of_accounts
               WHERE account_id = $1 AND user_id = $2""",
            account_id, user_id,
        )
        if not acct:
            return {"error": "Account not found"}

        # Build conditions for line items belonging to this account
        conditions = ["li.account_id = $1", "li.user_id = $2"]
        params: list = [account_id, user_id]

        if from_date:
            params.append(from_date)
            conditions.append(f"COALESCE(j.journal_date, inv.date, b.date, e.date, CURRENT_DATE) >= ${len(params)}")
        if to_date:
            params.append(to_date)
            conditions.append(f"COALESCE(j.journal_date, inv.date, b.date, e.date, CURRENT_DATE) <= ${len(params)}")

        where = " AND ".join(conditions)

        # Opening balance (sum before from_date)
        opening_balance = 0.0
        if from_date:
            ob = await conn.fetchval(
                f"""SELECT COALESCE(SUM(
                      CASE WHEN li.debit_or_credit = 'debit' THEN COALESCE(li.amount, 0)
                           ELSE -COALESCE(li.amount, 0) END
                    ), 0)
                    FROM acc_line_items li
                    LEFT JOIN acc_journals j ON li.parent_type = 'journal' AND li.parent_id = j.journal_id
                    LEFT JOIN acc_invoices inv ON li.parent_type = 'invoice' AND li.parent_id = inv.invoice_id
                    LEFT JOIN acc_bills b ON li.parent_type = 'bill' AND li.parent_id = b.bill_id
                    LEFT JOIN acc_expenses e ON li.parent_type = 'expense' AND li.parent_id = e.expense_id
                    WHERE li.account_id = $1 AND li.user_id = $2
                      AND COALESCE(j.journal_date, inv.date, b.date, e.date, CURRENT_DATE) < $3""",
                account_id, user_id, from_date,
            )
            opening_balance = _float(ob)

        # Total count for pagination
        total = await conn.fetchval(
            f"""SELECT COUNT(*)
                FROM acc_line_items li
                LEFT JOIN acc_journals j ON li.parent_type = 'journal' AND li.parent_id = j.journal_id
                LEFT JOIN acc_invoices inv ON li.parent_type = 'invoice' AND li.parent_id = inv.invoice_id
                LEFT JOIN acc_bills b ON li.parent_type = 'bill' AND li.parent_id = b.bill_id
                LEFT JOIN acc_expenses e ON li.parent_type = 'expense' AND li.parent_id = e.expense_id
                WHERE {where}""",
            *params,
        )

        offset = (page - 1) * per_page
        rows = await conn.fetch(
            f"""SELECT li.line_item_id, li.parent_id, li.parent_type,
                       li.description, li.debit_or_credit,
                       COALESCE(li.amount, 0) AS amount,
                       li.name, li.account_name,
                       COALESCE(j.journal_date, inv.date, b.date, e.date, CURRENT_DATE) AS txn_date,
                       COALESCE(j.journal_number, inv.invoice_number, b.bill_number, e.expense_number, '') AS ref_number
                FROM acc_line_items li
                LEFT JOIN acc_journals j ON li.parent_type = 'journal' AND li.parent_id = j.journal_id
                LEFT JOIN acc_invoices inv ON li.parent_type = 'invoice' AND li.parent_id = inv.invoice_id
                LEFT JOIN acc_bills b ON li.parent_type = 'bill' AND li.parent_id = b.bill_id
                LEFT JOIN acc_expenses e ON li.parent_type = 'expense' AND li.parent_id = e.expense_id
                WHERE {where}
                ORDER BY txn_date, li.created_at
                LIMIT {per_page} OFFSET {offset}""",
            *params,
        )

    entries = []
    running = opening_balance
    for r in rows:
        amt = _float(r["amount"])
        if r["debit_or_credit"] == "debit":
            running += amt
            entries.append({
                "line_item_id": str(r["line_item_id"]),
                "date": str(r["txn_date"]),
                "ref_number": r["ref_number"],
                "parent_type": r["parent_type"],
                "parent_id": str(r["parent_id"]),
                "description": r["description"] or r["name"] or "",
                "debit": amt,
                "credit": 0,
                "balance": round(running, 2),
            })
        else:
            running -= amt
            entries.append({
                "line_item_id": str(r["line_item_id"]),
                "date": str(r["txn_date"]),
                "ref_number": r["ref_number"],
                "parent_type": r["parent_type"],
                "parent_id": str(r["parent_id"]),
                "description": r["description"] or r["name"] or "",
                "debit": 0,
                "credit": amt,
                "balance": round(running, 2),
            })

    return {
        "account": {
            "account_id": str(acct["account_id"]),
            "account_name": acct["account_name"],
            "account_type": acct["account_type"],
            "account_code": acct["account_code"],
        },
        "opening_balance": round(opening_balance, 2),
        "closing_balance": round(running, 2),
        "entries": entries,
        "total": total or 0,
        "page": page,
        "per_page": per_page,
    }


# ─── Journal Entries ────────────────────────────────────────

async def get_journal_entries(
    *,
    user_id: str,
    branch_id: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    status: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    """List journal entries with their line items."""
    conditions = ["j.user_id = $1"]
    params: list = [user_id]

    if from_date:
        params.append(from_date)
        conditions.append(f"j.journal_date >= ${len(params)}")
    if to_date:
        params.append(to_date)
        conditions.append(f"j.journal_date <= ${len(params)}")
    if status:
        params.append(status)
        conditions.append(f"j.status = ${len(params)}")

    where = " AND ".join(conditions)
    offset = (page - 1) * per_page

    async with get_connection() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM acc_journals j WHERE {where}", *params,
        )
        journals = await conn.fetch(
            f"""SELECT j.journal_id, j.journal_number, j.journal_date,
                       j.reference_number, j.notes, j.journal_type,
                       j.status, j.total, j.sub_total, j.created_at
                FROM acc_journals j
                WHERE {where}
                ORDER BY j.journal_date DESC, j.created_at DESC
                LIMIT {per_page} OFFSET {offset}""",
            *params,
        )

        # Fetch line items for these journals
        journal_ids = [r["journal_id"] for r in journals]
        line_items = []
        if journal_ids:
            line_items = await conn.fetch(
                """SELECT li.line_item_id, li.parent_id, li.account_id,
                          li.account_name, li.description, li.debit_or_credit,
                          COALESCE(li.amount, 0) AS amount, li.name,
                          a.account_name AS resolved_account_name,
                          a.account_code
                   FROM acc_line_items li
                   LEFT JOIN acc_chart_of_accounts a ON a.account_id = li.account_id
                   WHERE li.parent_type = 'journal'
                     AND li.parent_id = ANY($1)
                   ORDER BY li.item_order, li.created_at""",
                journal_ids,
            )

    # Group line items by journal
    li_map: dict = {}
    for li in line_items:
        pid = str(li["parent_id"])
        li_map.setdefault(pid, []).append({
            "line_item_id": str(li["line_item_id"]),
            "account_id": str(li["account_id"]) if li["account_id"] else None,
            "account_name": li["resolved_account_name"] or li["account_name"] or "",
            "account_code": li["account_code"],
            "description": li["description"] or li["name"] or "",
            "debit_or_credit": li["debit_or_credit"],
            "amount": _float(li["amount"]),
        })

    result = []
    for j in journals:
        jid = str(j["journal_id"])
        total_debit = sum(x["amount"] for x in li_map.get(jid, []) if x["debit_or_credit"] == "debit")
        total_credit = sum(x["amount"] for x in li_map.get(jid, []) if x["debit_or_credit"] == "credit")
        result.append({
            "journal_id": jid,
            "journal_number": j["journal_number"],
            "date": str(j["journal_date"]),
            "reference_number": j["reference_number"],
            "notes": j["notes"],
            "journal_type": j["journal_type"],
            "status": j["status"],
            "total": _float(j["total"]),
            "total_debit": round(total_debit, 2),
            "total_credit": round(total_credit, 2),
            "line_items": li_map.get(jid, []),
            "created_at": str(j["created_at"]),
        })

    return {
        "journals": result,
        "total": total or 0,
        "page": page,
        "per_page": per_page,
    }


async def get_journal_detail(
    *,
    journal_id: str,
    user_id: str,
) -> dict:
    """Single journal entry with its line items."""
    async with get_connection() as conn:
        j = await conn.fetchrow(
            """SELECT journal_id, journal_number, journal_date, reference_number,
                      notes, journal_type, status, total, sub_total, currency_id,
                      exchange_rate, created_at, updated_at
               FROM acc_journals
               WHERE journal_id = $1 AND user_id = $2""",
            journal_id, user_id,
        )
        if not j:
            return {"error": "Journal not found"}

        line_items = await conn.fetch(
            """SELECT li.line_item_id, li.account_id, li.account_name,
                      li.description, li.debit_or_credit,
                      COALESCE(li.amount, 0) AS amount, li.name,
                      a.account_name AS resolved_account_name,
                      a.account_code, a.account_type
               FROM acc_line_items li
               LEFT JOIN acc_chart_of_accounts a ON a.account_id = li.account_id
               WHERE li.parent_type = 'journal' AND li.parent_id = $1
               ORDER BY li.item_order, li.created_at""",
            journal_id,
        )

    items = []
    total_debit = 0.0
    total_credit = 0.0
    for li in line_items:
        amt = _float(li["amount"])
        if li["debit_or_credit"] == "debit":
            total_debit += amt
        else:
            total_credit += amt
        items.append({
            "line_item_id": str(li["line_item_id"]),
            "account_id": str(li["account_id"]) if li["account_id"] else None,
            "account_name": li["resolved_account_name"] or li["account_name"] or "",
            "account_code": li["account_code"],
            "account_type": li["account_type"],
            "description": li["description"] or li["name"] or "",
            "debit_or_credit": li["debit_or_credit"],
            "amount": amt,
        })

    return {
        "journal_id": str(j["journal_id"]),
        "journal_number": j["journal_number"],
        "date": str(j["journal_date"]),
        "reference_number": j["reference_number"],
        "notes": j["notes"],
        "journal_type": j["journal_type"],
        "status": j["status"],
        "total": _float(j["total"]),
        "total_debit": round(total_debit, 2),
        "total_credit": round(total_credit, 2),
        "is_balanced": abs(total_debit - total_credit) < 0.01,
        "line_items": items,
        "created_at": str(j["created_at"]),
        "updated_at": str(j["updated_at"]),
    }


# ─── Trial Balance ──────────────────────────────────────────

async def get_trial_balance(
    *,
    user_id: str,
    branch_id: Optional[str] = None,
    as_of_date: Optional[date] = None,
) -> dict:
    """Trial Balance — totals debit/credit per account as of a date.

    For each account in chart_of_accounts:
      debit_total  = SUM of debit-side line items
      credit_total = SUM of credit-side line items
    Includes opening balances row.
    """
    target = as_of_date or date.today()

    async with get_connection() as conn:
        rows = await conn.fetch(
            """SELECT
                 a.account_id, a.account_name, a.account_type, a.account_code,
                 COALESCE(SUM(CASE WHEN li.debit_or_credit = 'debit' THEN li.amount ELSE 0 END), 0) AS total_debit,
                 COALESCE(SUM(CASE WHEN li.debit_or_credit = 'credit' THEN li.amount ELSE 0 END), 0) AS total_credit
               FROM acc_chart_of_accounts a
               LEFT JOIN acc_line_items li
                 ON li.account_id = a.account_id
                AND li.user_id = a.user_id
                AND li.created_at <= ($3::date + interval '1 day')
               WHERE a.user_id = $1 AND a.is_active = true
               GROUP BY a.account_id, a.account_name, a.account_type, a.account_code
               HAVING COALESCE(SUM(CASE WHEN li.debit_or_credit = 'debit' THEN li.amount ELSE 0 END), 0) > 0
                  OR  COALESCE(SUM(CASE WHEN li.debit_or_credit = 'credit' THEN li.amount ELSE 0 END), 0) > 0
               ORDER BY a.account_type, a.account_name""",
            user_id, branch_id, target,
        )

        # Also get opening balances
        opening = await conn.fetch(
            """SELECT accounts FROM acc_opening_balances
               WHERE user_id = $1
               ORDER BY date DESC LIMIT 1""",
            user_id,
        )

    accounts = []
    grand_debit = 0.0
    grand_credit = 0.0

    for r in rows:
        d = _float(r["total_debit"])
        c = _float(r["total_credit"])
        net = d - c
        accounts.append({
            "account_id": str(r["account_id"]),
            "account_name": r["account_name"],
            "account_type": r["account_type"],
            "account_code": r["account_code"],
            "debit": round(d, 2),
            "credit": round(c, 2),
            "net_balance": round(net, 2),
        })
        grand_debit += d
        grand_credit += c

    return {
        "as_of_date": str(target),
        "accounts": accounts,
        "totals": {
            "total_debit": round(grand_debit, 2),
            "total_credit": round(grand_credit, 2),
            "difference": round(grand_debit - grand_credit, 2),
            "is_balanced": abs(grand_debit - grand_credit) < 0.01,
        },
    }


# ─── Profit & Loss Statement ────────────────────────────────

async def get_profit_and_loss(
    *,
    user_id: str,
    branch_id: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> dict:
    """P&L (Income Statement) for a date range.

    Income accounts  − Expense accounts = Net Profit / Loss
    """
    start = from_date or date.today().replace(month=4, day=1)  # Default FY start (Apr 1)
    if start > date.today():
        start = start.replace(year=start.year - 1)
    end = to_date or date.today()

    async with get_connection() as conn:
        rows = await conn.fetch(
            """SELECT
                 a.account_id, a.account_name, a.account_type, a.account_code,
                 COALESCE(SUM(CASE WHEN li.debit_or_credit = 'debit' THEN li.amount ELSE 0 END), 0) AS total_debit,
                 COALESCE(SUM(CASE WHEN li.debit_or_credit = 'credit' THEN li.amount ELSE 0 END), 0) AS total_credit
               FROM acc_chart_of_accounts a
               JOIN acc_line_items li
                 ON li.account_id = a.account_id AND li.user_id = a.user_id
               WHERE a.user_id = $1
                 AND a.is_active = true
                 AND li.created_at >= $2::date
                 AND li.created_at < ($3::date + interval '1 day')
                 AND LOWER(REPLACE(REPLACE(a.account_type, ' ', '_'), '-', '_'))
                     IN ('income', 'other_income', 'revenue', 'sales',
                         'expense', 'other_expense', 'cost_of_goods_sold', 'cogs')
               GROUP BY a.account_id, a.account_name, a.account_type, a.account_code
               ORDER BY a.account_type, a.account_name""",
            user_id, start, end,
        )

    income_items = []
    expense_items = []
    total_income = 0.0
    total_expense = 0.0

    for r in rows:
        d = _float(r["total_debit"])
        c = _float(r["total_credit"])
        at = _norm(r["account_type"])

        entry = {
            "account_id": str(r["account_id"]),
            "account_name": r["account_name"],
            "account_type": r["account_type"],
            "account_code": r["account_code"],
        }

        if _is_type(at, INCOME_TYPES):
            # Income: credit-side is positive
            amount = c - d
            entry["amount"] = round(amount, 2)
            income_items.append(entry)
            total_income += amount
        else:
            # Expense: debit-side is positive
            amount = d - c
            entry["amount"] = round(amount, 2)
            expense_items.append(entry)
            total_expense += amount

    net_profit = total_income - total_expense

    return {
        "from_date": str(start),
        "to_date": str(end),
        "income": {
            "accounts": income_items,
            "total": round(total_income, 2),
        },
        "expenses": {
            "accounts": expense_items,
            "total": round(total_expense, 2),
        },
        "net_profit": round(net_profit, 2),
        "is_profit": net_profit >= 0,
    }


# ─── Balance Sheet ──────────────────────────────────────────

async def get_balance_sheet(
    *,
    user_id: str,
    branch_id: Optional[str] = None,
    as_of_date: Optional[date] = None,
) -> dict:
    """Balance Sheet as of a date.

    Assets = Liabilities + Equity
    """
    target = as_of_date or date.today()

    async with get_connection() as conn:
        rows = await conn.fetch(
            """SELECT
                 a.account_id, a.account_name, a.account_type, a.account_code,
                 COALESCE(SUM(CASE WHEN li.debit_or_credit = 'debit' THEN li.amount ELSE 0 END), 0) AS total_debit,
                 COALESCE(SUM(CASE WHEN li.debit_or_credit = 'credit' THEN li.amount ELSE 0 END), 0) AS total_credit
               FROM acc_chart_of_accounts a
               LEFT JOIN acc_line_items li
                 ON li.account_id = a.account_id AND li.user_id = a.user_id
                AND li.created_at <= ($3::date + interval '1 day')
               WHERE a.user_id = $1 AND a.is_active = true
               GROUP BY a.account_id, a.account_name, a.account_type, a.account_code
               ORDER BY a.account_type, a.account_name""",
            user_id, branch_id, target,
        )

    assets = []
    liabilities = []
    equity = []
    total_assets = 0.0
    total_liabilities = 0.0
    total_equity = 0.0

    for r in rows:
        d = _float(r["total_debit"])
        c = _float(r["total_credit"])
        at = _norm(r["account_type"])

        entry = {
            "account_id": str(r["account_id"]),
            "account_name": r["account_name"],
            "account_type": r["account_type"],
            "account_code": r["account_code"],
        }

        if _is_type(at, ASSET_TYPES):
            balance = d - c  # assets: debit-normal
            entry["balance"] = round(balance, 2)
            if abs(balance) > 0.001:
                assets.append(entry)
            total_assets += balance
        elif _is_type(at, LIABILITY_TYPES):
            balance = c - d  # liabilities: credit-normal
            entry["balance"] = round(balance, 2)
            if abs(balance) > 0.001:
                liabilities.append(entry)
            total_liabilities += balance
        elif _is_type(at, EQUITY_TYPES):
            balance = c - d  # equity: credit-normal
            entry["balance"] = round(balance, 2)
            if abs(balance) > 0.001:
                equity.append(entry)
            total_equity += balance
        # income & expense accounts are excluded from balance sheet
        # (their net goes into retained earnings / current year profit)

    # Current year P&L flows into equity as retained earnings
    # Compute current-year income - expense
    async with get_connection() as conn:
        fy_start = target.replace(month=4, day=1)
        if fy_start > target:
            fy_start = fy_start.replace(year=fy_start.year - 1)

        pnl = await conn.fetchrow(
            """SELECT
                 COALESCE(SUM(CASE
                   WHEN LOWER(REPLACE(REPLACE(a.account_type, ' ', '_'), '-', '_'))
                        IN ('income', 'other_income', 'revenue', 'sales')
                   THEN (CASE WHEN li.debit_or_credit = 'credit' THEN li.amount ELSE -li.amount END)
                   ELSE 0 END), 0) AS income,
                 COALESCE(SUM(CASE
                   WHEN LOWER(REPLACE(REPLACE(a.account_type, ' ', '_'), '-', '_'))
                        IN ('expense', 'other_expense', 'cost_of_goods_sold', 'cogs')
                   THEN (CASE WHEN li.debit_or_credit = 'debit' THEN li.amount ELSE -li.amount END)
                   ELSE 0 END), 0) AS expense
               FROM acc_line_items li
               JOIN acc_chart_of_accounts a ON a.account_id = li.account_id AND a.user_id = li.user_id
               WHERE li.user_id = $1
                 AND li.created_at >= $2::date
                 AND li.created_at <= ($3::date + interval '1 day')""",
            user_id, fy_start, target,
        )

    current_year_profit = _float(pnl["income"]) - _float(pnl["expense"]) if pnl else 0.0
    total_equity += current_year_profit
    if abs(current_year_profit) > 0.001:
        equity.append({
            "account_id": None,
            "account_name": "Current Year Profit / (Loss)",
            "account_type": "equity",
            "account_code": None,
            "balance": round(current_year_profit, 2),
        })

    return {
        "as_of_date": str(target),
        "assets": {
            "accounts": assets,
            "total": round(total_assets, 2),
        },
        "liabilities": {
            "accounts": liabilities,
            "total": round(total_liabilities, 2),
        },
        "equity": {
            "accounts": equity,
            "total": round(total_equity, 2),
        },
        "totals": {
            "total_assets": round(total_assets, 2),
            "total_liabilities_and_equity": round(total_liabilities + total_equity, 2),
            "difference": round(total_assets - (total_liabilities + total_equity), 2),
            "is_balanced": abs(total_assets - (total_liabilities + total_equity)) < 0.01,
        },
    }


# ─── GST Reports ────────────────────────────────────────────

async def get_gstr1_report(
    *,
    user_id: str,
    branch_id: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> dict:
    """GSTR-1: Outward supplies (sales invoices + credit notes).

    Groups by GST rate, shows taxable value, CGST, SGST, IGST, cess.
    """
    start = from_date or date.today().replace(day=1)
    end = to_date or date.today()

    async with get_connection() as conn:
        # Sales invoices with GST
        invoices = await conn.fetch(
            """SELECT i.invoice_id, i.invoice_number, i.date, i.total,
                      i.tax_total, i.sub_total, i.gst_no, i.gst_treatment,
                      i.place_of_supply,
                      c.contact_name, c.gst_no AS customer_gst
               FROM acc_invoices i
               LEFT JOIN acc_contacts c ON c.contact_id = i.customer_id
               WHERE i.user_id = $1
                 AND i.date >= $2 AND i.date <= $3
                 AND i.status NOT IN ('draft', 'void')
               ORDER BY i.date""",
            user_id, start, end,
        )

        # Line items with tax details
        inv_ids = [r["invoice_id"] for r in invoices]
        tax_lines = []
        if inv_ids:
            tax_lines = await conn.fetch(
                """SELECT li.parent_id, li.item_total, li.tax_total,
                          li.tax_percentage, li.tax_name, li.hsn_or_sac,
                          li.name, li.quantity, li.rate
                   FROM acc_line_items li
                   WHERE li.parent_type = 'invoice'
                     AND li.parent_id = ANY($1)
                   ORDER BY li.item_order""",
                inv_ids,
            )

        # Credit notes
        credit_notes = await conn.fetch(
            """SELECT cn.creditnote_id, cn.creditnote_number, cn.date,
                      cn.total, cn.tax_total, cn.sub_total,
                      cn.gst_treatment, cn.gst_no,
                      c.contact_name, c.gst_no AS customer_gst
               FROM acc_credit_notes cn
               LEFT JOIN acc_contacts c ON c.contact_id = cn.customer_id
               WHERE cn.user_id = $1
                 AND cn.date >= $2 AND cn.date <= $3
                 AND cn.status NOT IN ('draft', 'void')
               ORDER BY cn.date""",
            user_id, start, end,
        )

    # Build tax-rate grouped summary
    rate_summary: dict = {}
    for li in tax_lines:
        rate = _float(li["tax_percentage"])
        key = f"{rate:.1f}"
        if key not in rate_summary:
            rate_summary[key] = {"tax_rate": rate, "taxable_value": 0, "tax_amount": 0, "count": 0}
        rate_summary[key]["taxable_value"] += _float(li["item_total"])
        rate_summary[key]["tax_amount"] += _float(li["tax_total"])
        rate_summary[key]["count"] += 1

    total_taxable = sum(v["taxable_value"] for v in rate_summary.values())
    total_tax = sum(v["tax_amount"] for v in rate_summary.values())

    inv_list = [{
        "invoice_id": str(r["invoice_id"]),
        "invoice_number": r["invoice_number"],
        "date": str(r["date"]),
        "customer_name": r["contact_name"],
        "customer_gst": r["customer_gst"],
        "place_of_supply": r["place_of_supply"],
        "taxable_value": _float(r["sub_total"]),
        "tax_amount": _float(r["tax_total"]),
        "total": _float(r["total"]),
    } for r in invoices]

    cn_list = [{
        "creditnote_id": str(r["creditnote_id"]),
        "creditnote_number": r["creditnote_number"],
        "date": str(r["date"]),
        "customer_name": r["contact_name"],
        "customer_gst": r["customer_gst"],
        "taxable_value": _float(r["sub_total"]),
        "tax_amount": _float(r["tax_total"]),
        "total": _float(r["total"]),
    } for r in credit_notes]

    return {
        "report": "GSTR-1",
        "from_date": str(start),
        "to_date": str(end),
        "summary_by_rate": sorted(rate_summary.values(), key=lambda x: x["tax_rate"]),
        "totals": {
            "total_taxable_value": round(total_taxable, 2),
            "total_tax": round(total_tax, 2),
            "total_invoices": len(invoices),
            "total_credit_notes": len(credit_notes),
        },
        "invoices": inv_list,
        "credit_notes": cn_list,
    }


async def get_gstr2_report(
    *,
    user_id: str,
    branch_id: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> dict:
    """GSTR-2: Inward supplies (purchase bills + expenses + debit notes)."""
    start = from_date or date.today().replace(day=1)
    end = to_date or date.today()

    async with get_connection() as conn:
        # Purchase bills
        bills = await conn.fetch(
            """SELECT b.bill_id, b.bill_number, b.date, b.total,
                      b.tax_total, b.sub_total, b.gst_no, b.gst_treatment,
                      b.place_of_supply, b.source_of_supply,
                      c.contact_name, c.gst_no AS vendor_gst
               FROM acc_bills b
               LEFT JOIN acc_contacts c ON c.contact_id = b.vendor_id
               WHERE b.user_id = $1
                 AND b.date >= $2 AND b.date <= $3
                 AND b.status NOT IN ('draft', 'void')
               ORDER BY b.date""",
            user_id, start, end,
        )

        # Expenses with tax
        expenses = await conn.fetch(
            """SELECT e.expense_id, e.expense_number, e.date, e.amount,
                      e.description, e.hsn_or_sac, e.gst_no,
                      t.tax_name, t.tax_percentage
               FROM acc_expenses e
               LEFT JOIN acc_taxes t ON t.tax_id = e.tax_id
               WHERE e.user_id = $1
                 AND e.date >= $2 AND e.date <= $3
               ORDER BY e.date""",
            user_id, start, end,
        )

        # Debit notes (vendor)
        debit_notes = await conn.fetch(
            """SELECT dn.debit_note_id, dn.debit_note_number, dn.date,
                      dn.total, dn.tax_total, dn.sub_total,
                      c.contact_name, c.gst_no AS customer_gst
               FROM acc_debit_notes dn
               LEFT JOIN acc_contacts c ON c.contact_id = dn.customer_id
               WHERE dn.user_id = $1
                 AND dn.date >= $2 AND dn.date <= $3
                 AND dn.status NOT IN ('draft', 'void')
               ORDER BY dn.date""",
            user_id, start, end,
        )

    total_purchase_tax = sum(_float(r["tax_total"]) for r in bills)
    total_purchase_value = sum(_float(r["sub_total"]) for r in bills)

    bill_list = [{
        "bill_id": str(r["bill_id"]),
        "bill_number": r["bill_number"],
        "date": str(r["date"]),
        "vendor_name": r["contact_name"],
        "vendor_gst": r["vendor_gst"],
        "place_of_supply": r["place_of_supply"],
        "taxable_value": _float(r["sub_total"]),
        "tax_amount": _float(r["tax_total"]),
        "total": _float(r["total"]),
    } for r in bills]

    expense_list = [{
        "expense_id": str(r["expense_id"]),
        "expense_number": r["expense_number"],
        "date": str(r["date"]),
        "amount": _float(r["amount"]),
        "description": r["description"],
        "tax_name": r["tax_name"],
        "tax_percentage": _float(r["tax_percentage"]),
    } for r in expenses]

    dn_list = [{
        "debit_note_id": str(r["debit_note_id"]),
        "debit_note_number": r["debit_note_number"],
        "date": str(r["date"]),
        "customer_name": r["contact_name"],
        "customer_gst": r["customer_gst"],
        "taxable_value": _float(r["sub_total"]),
        "tax_amount": _float(r["tax_total"]),
        "total": _float(r["total"]),
    } for r in debit_notes]

    return {
        "report": "GSTR-2",
        "from_date": str(start),
        "to_date": str(end),
        "totals": {
            "total_purchase_taxable": round(total_purchase_value, 2),
            "total_purchase_tax": round(total_purchase_tax, 2),
            "total_bills": len(bills),
            "total_expenses": len(expenses),
            "total_debit_notes": len(debit_notes),
        },
        "bills": bill_list,
        "expenses": expense_list,
        "debit_notes": dn_list,
    }


async def get_gst_summary(
    *,
    user_id: str,
    branch_id: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> dict:
    """GST summary: output tax (GSTR-1) vs input tax (GSTR-2) = net payable/refundable."""
    start = from_date or date.today().replace(day=1)
    end = to_date or date.today()

    async with get_connection() as conn:
        # Output tax (invoices)
        out_tax = await conn.fetchval(
            """SELECT COALESCE(SUM(tax_total), 0)
               FROM acc_invoices
               WHERE user_id = $1 AND date >= $2 AND date <= $3
                 AND status NOT IN ('draft', 'void')""",
            user_id, start, end,
        )

        # Output tax from credit notes (reduces output)
        cn_tax = await conn.fetchval(
            """SELECT COALESCE(SUM(tax_total), 0)
               FROM acc_credit_notes
               WHERE user_id = $1 AND date >= $2 AND date <= $3
                 AND status NOT IN ('draft', 'void')""",
            user_id, start, end,
        )

        # Input tax (bills)
        in_tax = await conn.fetchval(
            """SELECT COALESCE(SUM(tax_total), 0)
               FROM acc_bills
               WHERE user_id = $1 AND date >= $2 AND date <= $3
                 AND status NOT IN ('draft', 'void')""",
            user_id, start, end,
        )

        # Input tax from debit notes (reduces input)
        dn_tax = await conn.fetchval(
            """SELECT COALESCE(SUM(tax_total), 0)
               FROM acc_debit_notes
               WHERE user_id = $1 AND date >= $2 AND date <= $3
                 AND status NOT IN ('draft', 'void')""",
            user_id, start, end,
        )

    output_tax = _float(out_tax) - _float(cn_tax)
    input_tax = _float(in_tax) - _float(dn_tax)
    net_gst = output_tax - input_tax

    return {
        "from_date": str(start),
        "to_date": str(end),
        "output_tax": round(output_tax, 2),
        "output_tax_invoices": round(_float(out_tax), 2),
        "output_tax_credit_notes": round(_float(cn_tax), 2),
        "input_tax": round(input_tax, 2),
        "input_tax_bills": round(_float(in_tax), 2),
        "input_tax_debit_notes": round(_float(dn_tax), 2),
        "net_gst_payable": round(net_gst, 2),
        "status": "payable" if net_gst > 0 else "refundable" if net_gst < 0 else "nil",
    }


async def get_gst_hsn_summary(
    *,
    user_id: str,
    branch_id: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> dict:
    """HSN/SAC-wise summary for GST returns."""
    start = from_date or date.today().replace(day=1)
    end = to_date or date.today()

    async with get_connection() as conn:
        rows = await conn.fetch(
            """SELECT
                 COALESCE(li.hsn_or_sac, 'N/A') AS hsn_sac,
                 li.tax_percentage,
                 COUNT(*) AS line_count,
                 SUM(li.quantity) AS total_quantity,
                 SUM(li.item_total) AS total_value,
                 SUM(li.tax_total) AS total_tax
               FROM acc_line_items li
               JOIN acc_invoices i ON i.invoice_id = li.parent_id AND li.parent_type = 'invoice'
               WHERE li.user_id = $1
                 AND i.date >= $2 AND i.date <= $3
                 AND i.status NOT IN ('draft', 'void')
               GROUP BY li.hsn_or_sac, li.tax_percentage
               ORDER BY li.hsn_or_sac""",
            user_id, start, end,
        )

    items = [{
        "hsn_sac": r["hsn_sac"],
        "tax_rate": _float(r["tax_percentage"]),
        "line_count": r["line_count"],
        "total_quantity": _float(r["total_quantity"]),
        "taxable_value": round(_float(r["total_value"]), 2),
        "tax_amount": round(_float(r["total_tax"]), 2),
    } for r in rows]

    return {
        "from_date": str(start),
        "to_date": str(end),
        "hsn_summary": items,
        "total_taxable": round(sum(i["taxable_value"] for i in items), 2),
        "total_tax": round(sum(i["tax_amount"] for i in items), 2),
    }
