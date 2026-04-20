"""
Finance Service — Product-level Financial Operating System
──────────────────────────────────────────────────────────
Beyond raw APIs, this provides opinionated workflows:

  1. Dashboard — 17 real-time metrics from ledger
  2. Daily Closing — Close day → verify cash → review → lock
  3. GST Filing Workflow — Generate → review → export → file → pay
  4. Insight Engine — "why profit dropped", channel analysis, comparisons
  5. Actionable Alerts — severity, suggested fixes, resolution workflow
  6. Trust Status — single-call system health for frontend badges
  7. Audit Log — with old/new value tracking
"""

from __future__ import annotations

import json as _json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional
from uuid import UUID

from app.core.database import get_connection
from app.core.logging import get_logger

logger = get_logger(__name__)


class FinanceService:
    """Singleton service powering /finance/* endpoints."""

    # ══════════════════════════════════════════════════════════════════════
    # DASHBOARD
    # ══════════════════════════════════════════════════════════════════════

    async def get_dashboard(
        self,
        restaurant_id: str,
        branch_id: Optional[str] = None,
    ) -> dict:
        """
        Real-time financial dashboard.

        Returns:
            today_revenue, month_revenue, net_income_mtd,
            cash_balance, bank_balance, card_balance,
            gst_payable, ar_outstanding, ap_outstanding,
            expense_mtd, cogs_mtd, food_cost_pct,
            trial_balance_ok, unreconciled_count,
            order_count_today, avg_order_value
        """
        rid = UUID(restaurant_id)
        bid = UUID(branch_id) if branch_id else None

        async with get_connection() as conn:
            # ── Revenue today & MTD ──
            branch_clause = "AND je.branch_id = $3" if bid else ""
            params_today: list = [rid, date.today()]
            params_mtd: list = [rid, date.today().replace(day=1), date.today()]
            if bid:
                params_today.append(bid)
                params_mtd.append(bid)

            today_rev = await conn.fetchval(f"""
                SELECT COALESCE(SUM(jl.credit), 0)
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND je.entry_date = $2
                  AND je.is_reversed = false
                  AND coa.system_code = 'SALES_REVENUE'
                  {branch_clause}
            """, *params_today)

            month_rev = await conn.fetchval(f"""
                SELECT COALESCE(SUM(jl.credit), 0)
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND je.entry_date BETWEEN $2 AND $3
                  AND je.is_reversed = false
                  AND coa.system_code = 'SALES_REVENUE'
                  {"AND je.branch_id = $4" if bid else ""}
            """, *params_mtd)

            # ── Account balances (cash, bank, card) ──
            bal_query = """
                SELECT coa.system_code,
                       COALESCE(SUM(jl.debit - jl.credit), 0) AS balance
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND je.is_reversed = false
                  AND coa.system_code IN ('CASH_ACCOUNT', 'BANK', 'CARD')
                GROUP BY coa.system_code
            """
            bal_rows = await conn.fetch(bal_query, rid)
            balances = {r["system_code"]: float(Decimal(str(r["balance"]))) for r in bal_rows}

            # ── GST payable ──
            gst = await conn.fetchval("""
                SELECT COALESCE(SUM(jl.credit - jl.debit), 0)
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND je.is_reversed = false
                  AND coa.system_code IN ('CGST_PAYABLE', 'SGST_PAYABLE', 'IGST_PAYABLE',
                                           'GST_OUTPUT', 'TAX_PAYABLE')
            """, rid)

            # ── AR outstanding ──
            ar = await conn.fetchval("""
                SELECT COALESCE(SUM(jl.debit - jl.credit), 0)
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND je.is_reversed = false
                  AND coa.system_code = 'ACCOUNTS_RECEIVABLE'
            """, rid)

            # ── AP outstanding ──
            ap = await conn.fetchval("""
                SELECT COALESCE(SUM(jl.credit - jl.debit), 0)
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND je.is_reversed = false
                  AND coa.system_code = 'ACCOUNTS_PAYABLE'
            """, rid)

            # ── COGS & Expenses MTD ──
            cogs = await conn.fetchval(f"""
                SELECT COALESCE(SUM(jl.debit), 0)
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND je.entry_date BETWEEN $2 AND $3
                  AND je.is_reversed = false
                  AND coa.system_code IN ('COGS_FOOD', 'COGS_BEVERAGE')
                  {"AND je.branch_id = $4" if bid else ""}
            """, *params_mtd)

            expense_mtd = await conn.fetchval(f"""
                SELECT COALESCE(SUM(jl.debit - jl.credit), 0)
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND je.entry_date BETWEEN $2 AND $3
                  AND je.is_reversed = false
                  AND coa.account_type = 'expense'
                  {"AND je.branch_id = $4" if bid else ""}
            """, *params_mtd)

            # ── Trial balance check ──
            tb = await conn.fetchrow("""
                SELECT COALESCE(SUM(jl.debit), 0) AS td,
                       COALESCE(SUM(jl.credit), 0) AS tc
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                WHERE je.restaurant_id = $1
            """, rid)

            # ── Unreconciled bank lines ──
            unrec = await conn.fetchval("""
                SELECT COUNT(*) FROM bank_statements
                WHERE restaurant_id = $1 AND reconciled = false AND excluded = false
            """, rid)

            # ── Order metrics today ──
            order_stats = await conn.fetchrow(f"""
                SELECT COUNT(DISTINCT je.reference_id) AS cnt,
                       COALESCE(SUM(jl.credit), 0)    AS total
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND je.entry_date = $2
                  AND je.is_reversed = false
                  AND je.reference_type = 'order'
                  AND coa.system_code = 'SALES_REVENUE'
                  {branch_clause}
            """, *params_today)

        today_revenue = float(Decimal(str(today_rev)))
        month_revenue = float(Decimal(str(month_rev)))
        cogs_val = float(Decimal(str(cogs)))
        expense_val = float(Decimal(str(expense_mtd)))
        net_income = month_revenue - expense_val
        food_cost_pct = round((cogs_val / month_revenue * 100), 1) if month_revenue > 0 else 0.0
        order_cnt = order_stats["cnt"] if order_stats else 0
        order_total = float(Decimal(str(order_stats["total"]))) if order_stats else 0.0

        return {
            "date": date.today().isoformat(),
            "today_revenue": today_revenue,
            "month_revenue": month_revenue,
            "net_income_mtd": net_income,
            "cash_balance": balances.get("CASH_ACCOUNT", 0.0),
            "bank_balance": balances.get("BANK", 0.0),
            "card_balance": balances.get("CARD", 0.0),
            "gst_payable": float(Decimal(str(gst))),
            "ar_outstanding": float(Decimal(str(ar))),
            "ap_outstanding": float(Decimal(str(ap))),
            "expense_mtd": expense_val,
            "cogs_mtd": cogs_val,
            "food_cost_pct": food_cost_pct,
            "trial_balance_ok": abs(float(tb["td"]) - float(tb["tc"])) < 0.01,
            "unreconciled_count": unrec or 0,
            "order_count_today": order_cnt,
            "avg_order_value": round(order_total / order_cnt, 2) if order_cnt > 0 else 0.0,
        }

    # ══════════════════════════════════════════════════════════════════════
    # ALERTS
    # ══════════════════════════════════════════════════════════════════════

    async def scan_alerts(self, restaurant_id: str) -> int:
        """Run the DB-side alert scanner and return count of new alerts."""
        rid = UUID(restaurant_id)
        async with get_connection() as conn:
            return await conn.fetchval(
                "SELECT fn_scan_financial_alerts($1)", rid
            )

    async def list_alerts(
        self,
        restaurant_id: str,
        *,
        resolved: Optional[bool] = None,
        alert_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List financial alerts for a restaurant."""
        rid = UUID(restaurant_id)
        clauses = ["restaurant_id = $1"]
        params: list = [rid]
        idx = 2

        if resolved is not None:
            clauses.append(f"is_resolved = ${idx}")
            params.append(resolved)
            idx += 1

        if alert_type:
            clauses.append(f"alert_type = ${idx}")
            params.append(alert_type)
            idx += 1

        where = " AND ".join(clauses)
        clauses_limit = f"LIMIT ${idx} OFFSET ${idx + 1}"
        params.extend([limit, offset])

        async with get_connection() as conn:
            rows = await conn.fetch(f"""
                SELECT id, restaurant_id, branch_id, alert_type, severity,
                       title, details, is_resolved, resolved_by, resolved_at, created_at
                FROM financial_alerts
                WHERE {where}
                ORDER BY created_at DESC
                {clauses_limit}
            """, *params)

        return [
            {
                "id": str(r["id"]),
                "alert_type": r["alert_type"],
                "severity": r["severity"],
                "title": r["title"],
                "details": dict(r["details"]) if r["details"] else {},
                "is_resolved": r["is_resolved"],
                "resolved_by": r["resolved_by"],
                "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]

    async def resolve_alert(
        self, restaurant_id: str, alert_id: str, user_id: str,
    ) -> bool:
        """Mark an alert as resolved."""
        rid = UUID(restaurant_id)
        aid = UUID(alert_id)
        async with get_connection() as conn:
            tag = await conn.execute("""
                UPDATE financial_alerts
                SET is_resolved = true, resolved_by = $3, resolved_at = NOW()
                WHERE id = $2 AND restaurant_id = $1 AND is_resolved = false
            """, rid, aid, user_id)
        return tag == "UPDATE 1"

    # ══════════════════════════════════════════════════════════════════════
    # MATERIALIZED VIEWS
    # ══════════════════════════════════════════════════════════════════════

    async def refresh_views(self) -> None:
        """Refresh all financial materialized views."""
        async with get_connection() as conn:
            await conn.execute("SELECT fn_refresh_financial_views()")

    # ══════════════════════════════════════════════════════════════════════
    # FINANCIAL AUDIT LOG
    # ══════════════════════════════════════════════════════════════════════

    async def log_financial_action(
        self,
        restaurant_id: str,
        user_id: str,
        action: str,
        entity_type: str,
        entity_id: Optional[str] = None,
        *,
        old_value: Optional[dict] = None,
        new_value: Optional[dict] = None,
        metadata: Optional[dict] = None,
        ip_address: Optional[str] = None,
    ) -> str:
        """Insert a row into financial_audit_log with old/new values."""
        import json
        rid = UUID(restaurant_id)
        async with get_connection() as conn:
            row_id = await conn.fetchval("""
                INSERT INTO financial_audit_log
                    (restaurant_id, user_id, action, entity_type, entity_id,
                     old_value, new_value, metadata, ip_address)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb, $9)
                RETURNING id
            """,
                rid, user_id, action, entity_type, entity_id,
                json.dumps(old_value) if old_value else None,
                json.dumps(new_value) if new_value else None,
                json.dumps(metadata or {}),
                ip_address,
            )
        return str(row_id)

    async def list_audit_log(
        self,
        restaurant_id: str,
        *,
        entity_type: Optional[str] = None,
        action: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Query the financial audit log."""
        rid = UUID(restaurant_id)
        clauses = ["restaurant_id = $1"]
        params: list = [rid]
        idx = 2

        if entity_type:
            clauses.append(f"entity_type = ${idx}")
            params.append(entity_type)
            idx += 1
        if action:
            clauses.append(f"action = ${idx}")
            params.append(action)
            idx += 1

        where = " AND ".join(clauses)
        params.extend([limit, offset])

        async with get_connection() as conn:
            rows = await conn.fetch(f"""
                SELECT id, user_id, action, entity_type, entity_id,
                       old_value, new_value, metadata, ip_address, created_at
                FROM financial_audit_log
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ${idx} OFFSET ${idx + 1}
            """, *params)

        return [
            {
                "id": str(r["id"]),
                "user_id": r["user_id"],
                "action": r["action"],
                "entity_type": r["entity_type"],
                "entity_id": r["entity_id"],
                "old_value": dict(r["old_value"]) if r["old_value"] else None,
                "new_value": dict(r["new_value"]) if r["new_value"] else None,
                "metadata": dict(r["metadata"]) if r["metadata"] else {},
                "ip_address": r["ip_address"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]

    # ══════════════════════════════════════════════════════════════════════
    # DAILY TREND  (uses mv_daily_revenue if refreshed)
    # ══════════════════════════════════════════════════════════════════════

    async def daily_revenue_trend(
        self,
        restaurant_id: str,
        from_date: date,
        to_date: date,
        branch_id: Optional[str] = None,
    ) -> list[dict]:
        """Return per-day revenue totals for chart/graph rendering."""
        rid = UUID(restaurant_id)
        bid = UUID(branch_id) if branch_id else None

        query = """
            SELECT je.entry_date,
                   COALESCE(SUM(jl.credit), 0) AS revenue
            FROM journal_lines jl
            JOIN journal_entries je ON je.id = jl.journal_entry_id
            JOIN chart_of_accounts coa ON coa.id = jl.account_id
            WHERE je.restaurant_id = $1
              AND je.entry_date BETWEEN $2 AND $3
              AND je.is_reversed = false
              AND coa.system_code = 'SALES_REVENUE'
        """
        params: list = [rid, from_date, to_date]
        if bid:
            query += " AND je.branch_id = $4"
            params.append(bid)
        query += " GROUP BY je.entry_date ORDER BY je.entry_date"

        async with get_connection() as conn:
            rows = await conn.fetch(query, *params)

        return [
            {"date": r["entry_date"].isoformat(), "revenue": float(r["revenue"])}
            for r in rows
        ]

    # ══════════════════════════════════════════════════════════════════════
    # DAILY CLOSING WORKFLOW
    # ══════════════════════════════════════════════════════════════════════

    async def init_daily_closing(
        self,
        restaurant_id: str,
        closing_date: date,
        branch_id: Optional[str] = None,
    ) -> dict:
        """
        Initialize a daily closing: compute expected cash/card/UPI from
        ledger, pull order count, revenue, refunds, discounts, expenses.
        Returns a summary ready for the cashier to verify.
        """
        rid = UUID(restaurant_id)
        bid = UUID(branch_id) if branch_id else None
        bc = "AND je.branch_id = $2" if bid else ""
        params: list = [rid]
        if bid:
            params.append(bid)

        async with get_connection() as conn:
            # Check if already exists
            existing = await conn.fetchrow("""
                SELECT id, status FROM daily_closings
                WHERE restaurant_id = $1 AND closing_date = $2
                  AND (branch_id = $3 OR ($3::uuid IS NULL AND branch_id IS NULL))
            """, rid, closing_date, bid)

            if existing and existing["status"] == "closed":
                return {"error": "Day already closed", "id": str(existing["id"]), "status": "closed"}

            # Expected cash from ledger
            exp_cash = await conn.fetchval(f"""
                SELECT COALESCE(SUM(jl.debit - jl.credit), 0)
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND je.entry_date = ${len(params) + 1}
                  AND je.is_reversed = false
                  AND coa.system_code = 'CASH_ACCOUNT'
                  {bc}
            """, *params, closing_date)

            exp_card = await conn.fetchval(f"""
                SELECT COALESCE(SUM(jl.debit - jl.credit), 0)
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND je.entry_date = ${len(params) + 1}
                  AND je.is_reversed = false
                  AND coa.system_code = 'CARD_ACCOUNT'
                  {bc}
            """, *params, closing_date)

            exp_upi = await conn.fetchval(f"""
                SELECT COALESCE(SUM(jl.debit - jl.credit), 0)
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND je.entry_date = ${len(params) + 1}
                  AND je.is_reversed = false
                  AND coa.system_code = 'UPI_ACCOUNT'
                  {bc}
            """, *params, closing_date)

            # Revenue, orders, refunds, discounts, expenses
            revenue = await conn.fetchval(f"""
                SELECT COALESCE(SUM(jl.credit), 0)
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1 AND je.entry_date = ${len(params) + 1}
                  AND je.is_reversed = false AND coa.system_code = 'SALES_REVENUE'
                  {bc}
            """, *params, closing_date)

            order_count = await conn.fetchval(f"""
                SELECT COUNT(DISTINCT je.reference_id)
                FROM journal_entries je
                WHERE je.restaurant_id = $1 AND je.entry_date = ${len(params) + 1}
                  AND je.is_reversed = false AND je.reference_type = 'order'
                  {bc}
            """, *params, closing_date)

            refunds = await conn.fetchval(f"""
                SELECT COALESCE(SUM(jl.debit), 0)
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1 AND je.entry_date = ${len(params) + 1}
                  AND je.reference_type = 'refund' AND je.is_reversed = false
                  AND coa.system_code = 'SALES_REVENUE'
                  {bc}
            """, *params, closing_date)

            discounts = await conn.fetchval(f"""
                SELECT COALESCE(SUM(jl.debit), 0)
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1 AND je.entry_date = ${len(params) + 1}
                  AND je.is_reversed = false AND coa.system_code = 'DISCOUNT_EXPENSE'
                  {bc}
            """, *params, closing_date)

            expenses = await conn.fetchval(f"""
                SELECT COALESCE(SUM(jl.debit - jl.credit), 0)
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1 AND je.entry_date = ${len(params) + 1}
                  AND je.is_reversed = false AND coa.account_type = 'expense'
                  {bc}
            """, *params, closing_date)

            ec = float(Decimal(str(exp_cash)))
            ecd = float(Decimal(str(exp_card)))
            eu = float(Decimal(str(exp_upi)))
            rev = float(Decimal(str(revenue)))
            ref = float(Decimal(str(refunds)))
            disc = float(Decimal(str(discounts)))
            exp = float(Decimal(str(expenses)))

            # Upsert daily closing
            row = await conn.fetchrow("""
                INSERT INTO daily_closings
                    (restaurant_id, branch_id, closing_date, status,
                     expected_cash, expected_card, expected_upi,
                     total_orders, total_revenue, total_refunds,
                     total_discounts, total_expenses, net_revenue)
                VALUES ($1, $2, $3, 'open', $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (restaurant_id, branch_id, closing_date)
                DO UPDATE SET
                    expected_cash = $4, expected_card = $5, expected_upi = $6,
                    total_orders = $7, total_revenue = $8, total_refunds = $9,
                    total_discounts = $10, total_expenses = $11, net_revenue = $12
                RETURNING id, status
            """, rid, bid, closing_date,
                ec, ecd, eu, order_count or 0, rev, ref, disc, exp, rev - ref - disc)

        return {
            "id": str(row["id"]),
            "status": row["status"],
            "closing_date": closing_date.isoformat(),
            "expected_cash": ec,
            "expected_card": ecd,
            "expected_upi": eu,
            "total_orders": order_count or 0,
            "total_revenue": rev,
            "total_refunds": ref,
            "total_discounts": disc,
            "total_expenses": exp,
            "net_revenue": rev - ref - disc,
        }

    async def submit_cash_count(
        self,
        restaurant_id: str,
        closing_id: str,
        user_id: str,
        actual_cash: float,
        actual_card: float,
        actual_upi: float,
        notes: Optional[str] = None,
    ) -> dict:
        """Cashier submits actual counted amounts."""
        rid = UUID(restaurant_id)
        cid = UUID(closing_id)
        async with get_connection() as conn:
            row = await conn.fetchrow("""
                UPDATE daily_closings
                SET actual_cash = $3, actual_card = $4, actual_upi = $5,
                    cash_difference = $3 - expected_cash,
                    card_difference = $4 - expected_card,
                    upi_difference = $5 - expected_upi,
                    status = 'cash_counted',
                    counted_by = $6, counted_at = NOW(),
                    notes = COALESCE($7, notes)
                WHERE id = $2 AND restaurant_id = $1
                  AND status IN ('open', 'cash_counted')
                RETURNING *
            """, rid, cid, actual_cash, actual_card, actual_upi, user_id, notes)

        if not row:
            return {"error": "Closing not found or already reviewed/closed"}

        return {
            "id": str(row["id"]),
            "status": row["status"],
            "cash_difference": float(row["cash_difference"]),
            "card_difference": float(row["card_difference"]),
            "upi_difference": float(row["upi_difference"]),
            "total_difference": float(row["cash_difference"] + row["card_difference"] + row["upi_difference"]),
            "mismatch": abs(float(row["cash_difference"])) > 0.01
                        or abs(float(row["card_difference"])) > 0.01
                        or abs(float(row["upi_difference"])) > 0.01,
        }

    async def close_day(
        self,
        restaurant_id: str,
        closing_id: str,
        user_id: str,
    ) -> dict:
        """Manager closes the day — locks it, produces summary."""
        rid = UUID(restaurant_id)
        cid = UUID(closing_id)
        async with get_connection() as conn:
            row = await conn.fetchrow("""
                UPDATE daily_closings
                SET status = 'closed',
                    closed_by = $3, closed_at = NOW(),
                    period_locked = true
                WHERE id = $2 AND restaurant_id = $1
                  AND status IN ('cash_counted', 'reviewed')
                RETURNING *
            """, rid, cid, user_id)

        if not row:
            return {"error": "Cannot close — cash count not submitted or already closed"}

        return {
            "id": str(row["id"]),
            "status": "closed",
            "closing_date": row["closing_date"].isoformat(),
            "summary": {
                "total_orders": row["total_orders"],
                "total_revenue": float(row["total_revenue"]),
                "total_refunds": float(row["total_refunds"]),
                "total_discounts": float(row["total_discounts"]),
                "total_expenses": float(row["total_expenses"]),
                "net_revenue": float(row["net_revenue"]),
                "expected_cash": float(row["expected_cash"]),
                "actual_cash": float(row["actual_cash"]) if row["actual_cash"] else None,
                "cash_difference": float(row["cash_difference"]) if row["cash_difference"] else None,
                "expected_card": float(row["expected_card"]),
                "actual_card": float(row["actual_card"]) if row["actual_card"] else None,
                "card_difference": float(row["card_difference"]) if row["card_difference"] else None,
                "expected_upi": float(row["expected_upi"]),
                "actual_upi": float(row["actual_upi"]) if row["actual_upi"] else None,
                "upi_difference": float(row["upi_difference"]) if row["upi_difference"] else None,
            },
            "closed_by": user_id,
        }

    async def list_closings(
        self,
        restaurant_id: str,
        *,
        branch_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 30,
        offset: int = 0,
    ) -> list[dict]:
        """List daily closings for a restaurant."""
        rid = UUID(restaurant_id)
        clauses = ["restaurant_id = $1"]
        params: list = [rid]
        idx = 2

        if branch_id:
            clauses.append(f"branch_id = ${idx}")
            params.append(UUID(branch_id))
            idx += 1
        if status:
            clauses.append(f"status = ${idx}")
            params.append(status)
            idx += 1

        where = " AND ".join(clauses)
        params.extend([limit, offset])

        async with get_connection() as conn:
            rows = await conn.fetch(f"""
                SELECT * FROM daily_closings
                WHERE {where}
                ORDER BY closing_date DESC
                LIMIT ${idx} OFFSET ${idx + 1}
            """, *params)

        result = []
        for r in rows:
            d = dict(r)
            d["id"] = str(d["id"])
            d["restaurant_id"] = str(d["restaurant_id"])
            if d.get("branch_id"):
                d["branch_id"] = str(d["branch_id"])
            d["closing_date"] = d["closing_date"].isoformat()
            d["created_at"] = d["created_at"].isoformat()
            for k in ("counted_at", "reviewed_at", "closed_at"):
                if d.get(k):
                    d[k] = d[k].isoformat()
            for k in ("expected_cash", "expected_card", "expected_upi",
                       "actual_cash", "actual_card", "actual_upi",
                       "cash_difference", "card_difference", "upi_difference",
                       "total_revenue", "total_refunds", "total_discounts",
                       "total_expenses", "net_revenue"):
                if d.get(k) is not None:
                    d[k] = float(d[k])
            result.append(d)
        return result

    # ══════════════════════════════════════════════════════════════════════
    # GST FILING WORKFLOW
    # ══════════════════════════════════════════════════════════════════════

    async def gst_workflow_generate(
        self,
        restaurant_id: str,
        period_start: date,
        period_end: date,
    ) -> dict:
        """Step 1: Generate GST data for a period."""
        rid = UUID(restaurant_id)
        async with get_connection() as conn:
            # Pull GST data from ledger
            gst = await conn.fetch("""
                SELECT coa.system_code,
                       COALESCE(SUM(jl.credit - jl.debit), 0) AS amount
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND je.entry_date BETWEEN $2 AND $3
                  AND je.is_reversed = false
                  AND coa.system_code IN ('CGST_PAYABLE', 'SGST_PAYABLE', 'IGST_PAYABLE',
                                          'CGST_INPUT', 'SGST_INPUT', 'IGST_INPUT')
                GROUP BY coa.system_code
            """, rid, period_start, period_end)

            gst_map = {r["system_code"]: float(Decimal(str(r["amount"]))) for r in gst}

            cgst_out = gst_map.get("CGST_PAYABLE", 0)
            sgst_out = gst_map.get("SGST_PAYABLE", 0)
            igst_out = gst_map.get("IGST_PAYABLE", 0)
            cgst_in = gst_map.get("CGST_INPUT", 0)
            sgst_in = gst_map.get("SGST_INPUT", 0)
            igst_in = gst_map.get("IGST_INPUT", 0)
            net = (cgst_out + sgst_out + igst_out) - (cgst_in + sgst_in + igst_in)

            row = await conn.fetchrow("""
                INSERT INTO gst_filing_workflows
                    (restaurant_id, period_start, period_end, status,
                     cgst_collected, sgst_collected, igst_collected,
                     cgst_input, sgst_input, igst_input,
                     net_payable, generated_at)
                VALUES ($1, $2, $3, 'generated', $4, $5, $6, $7, $8, $9, $10, NOW())
                ON CONFLICT (restaurant_id, period_start, period_end)
                DO UPDATE SET
                    cgst_collected = $4, sgst_collected = $5, igst_collected = $6,
                    cgst_input = $7, sgst_input = $8, igst_input = $9,
                    net_payable = $10, generated_at = NOW(),
                    status = CASE WHEN gst_filing_workflows.status IN ('draft', 'generated')
                                  THEN 'generated' ELSE gst_filing_workflows.status END
                RETURNING *
            """, rid, period_start, period_end,
                cgst_out, sgst_out, igst_out, cgst_in, sgst_in, igst_in, net)

        return self._gst_row_to_dict(row)

    async def gst_workflow_review(
        self,
        restaurant_id: str,
        workflow_id: str,
        user_id: str,
    ) -> dict:
        """Step 2: Mark GST filing as reviewed."""
        rid = UUID(restaurant_id)
        wid = UUID(workflow_id)
        async with get_connection() as conn:
            row = await conn.fetchrow("""
                UPDATE gst_filing_workflows
                SET status = 'reviewed', reviewed_by = $3, reviewed_at = NOW()
                WHERE id = $2 AND restaurant_id = $1 AND status = 'generated'
                RETURNING *
            """, rid, wid, user_id)
        if not row:
            return {"error": "Workflow not found or not in generated state"}
        return self._gst_row_to_dict(row)

    async def gst_workflow_export(
        self,
        restaurant_id: str,
        workflow_id: str,
    ) -> dict:
        """Step 3: Mark as exported (data pulled for filing)."""
        rid = UUID(restaurant_id)
        wid = UUID(workflow_id)
        async with get_connection() as conn:
            row = await conn.fetchrow("""
                UPDATE gst_filing_workflows
                SET status = 'exported', exported_at = NOW()
                WHERE id = $2 AND restaurant_id = $1 AND status = 'reviewed'
                RETURNING *
            """, rid, wid)
        if not row:
            return {"error": "Must be reviewed before export"}
        return self._gst_row_to_dict(row)

    async def gst_workflow_file(
        self,
        restaurant_id: str,
        workflow_id: str,
        filed_reference: str,
    ) -> dict:
        """Step 4: Mark as filed on GST portal."""
        rid = UUID(restaurant_id)
        wid = UUID(workflow_id)
        async with get_connection() as conn:
            row = await conn.fetchrow("""
                UPDATE gst_filing_workflows
                SET status = 'filed', filed_at = NOW(), filed_reference = $3
                WHERE id = $2 AND restaurant_id = $1 AND status = 'exported'
                RETURNING *
            """, rid, wid, filed_reference)
        if not row:
            return {"error": "Must be exported before filing"}
        return self._gst_row_to_dict(row)

    async def gst_workflow_pay(
        self,
        restaurant_id: str,
        workflow_id: str,
        paid_amount: float,
        paid_reference: str,
    ) -> dict:
        """Step 5: Mark GST as paid."""
        rid = UUID(restaurant_id)
        wid = UUID(workflow_id)
        async with get_connection() as conn:
            row = await conn.fetchrow("""
                UPDATE gst_filing_workflows
                SET status = 'paid', paid_at = NOW(),
                    paid_amount = $3, paid_reference = $4
                WHERE id = $2 AND restaurant_id = $1 AND status = 'filed'
                RETURNING *
            """, rid, wid, paid_amount, paid_reference)
        if not row:
            return {"error": "Must be filed before payment"}
        return self._gst_row_to_dict(row)

    async def list_gst_workflows(
        self,
        restaurant_id: str,
        *,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        rid = UUID(restaurant_id)
        clauses = ["restaurant_id = $1"]
        params: list = [rid]
        idx = 2
        if status:
            clauses.append(f"status = ${idx}")
            params.append(status)
            idx += 1
        where = " AND ".join(clauses)
        params.extend([limit, offset])
        async with get_connection() as conn:
            rows = await conn.fetch(f"""
                SELECT * FROM gst_filing_workflows
                WHERE {where}
                ORDER BY period_start DESC
                LIMIT ${idx} OFFSET ${idx + 1}
            """, *params)
        return [self._gst_row_to_dict(r) for r in rows]

    def _gst_row_to_dict(self, r) -> dict:
        result = {
            "id": str(r["id"]),
            "period_start": r["period_start"].isoformat(),
            "period_end": r["period_end"].isoformat(),
            "status": r["status"],
            "cgst_collected": float(r["cgst_collected"]),
            "sgst_collected": float(r["sgst_collected"]),
            "igst_collected": float(r["igst_collected"]),
            "cgst_input": float(r["cgst_input"]),
            "sgst_input": float(r["sgst_input"]),
            "igst_input": float(r["igst_input"]),
            "net_payable": float(r["net_payable"]),
        }
        for k in ("generated_at", "reviewed_at", "exported_at", "filed_at", "paid_at", "created_at"):
            result[k] = r[k].isoformat() if r.get(k) else None
        result["filed_reference"] = r.get("filed_reference")
        result["paid_amount"] = float(r["paid_amount"]) if r.get("paid_amount") else None
        result["paid_reference"] = r.get("paid_reference")
        return result

    # ══════════════════════════════════════════════════════════════════════
    # INSIGHT ENGINE — "WHY" answers, not just "WHAT"
    # ══════════════════════════════════════════════════════════════════════

    async def profit_insight(
        self,
        restaurant_id: str,
        target_date: date,
        branch_id: Optional[str] = None,
    ) -> dict:
        """
        Answer: "Why did profit change?"
        Compares target_date vs same day last week, breaks down by channel.
        """
        rid = UUID(restaurant_id)
        bid = UUID(branch_id) if branch_id else None
        compare_date = target_date - timedelta(days=7)
        bc = "AND je.branch_id = $3" if bid else ""

        async with get_connection() as conn:
            async def _day_breakdown(d: date) -> dict:
                p: list = [rid, d]
                if bid:
                    p.append(bid)

                rev = await conn.fetchval(f"""
                    SELECT COALESCE(SUM(jl.credit), 0)
                    FROM journal_lines jl
                    JOIN journal_entries je ON je.id = jl.journal_entry_id
                    JOIN chart_of_accounts coa ON coa.id = jl.account_id
                    WHERE je.restaurant_id = $1 AND je.entry_date = $2
                      AND je.is_reversed = false AND coa.system_code = 'SALES_REVENUE'
                      {bc}
                """, *p)

                cogs = await conn.fetchval(f"""
                    SELECT COALESCE(SUM(jl.debit), 0)
                    FROM journal_lines jl
                    JOIN journal_entries je ON je.id = jl.journal_entry_id
                    JOIN chart_of_accounts coa ON coa.id = jl.account_id
                    WHERE je.restaurant_id = $1 AND je.entry_date = $2
                      AND je.is_reversed = false
                      AND coa.system_code IN ('COGS_FOOD', 'COGS_BEVERAGE')
                      {bc}
                """, *p)

                expenses = await conn.fetchval(f"""
                    SELECT COALESCE(SUM(jl.debit - jl.credit), 0)
                    FROM journal_lines jl
                    JOIN journal_entries je ON je.id = jl.journal_entry_id
                    JOIN chart_of_accounts coa ON coa.id = jl.account_id
                    WHERE je.restaurant_id = $1 AND je.entry_date = $2
                      AND je.is_reversed = false AND coa.account_type = 'expense'
                      {bc}
                """, *p)

                discounts = await conn.fetchval(f"""
                    SELECT COALESCE(SUM(jl.debit), 0)
                    FROM journal_lines jl
                    JOIN journal_entries je ON je.id = jl.journal_entry_id
                    JOIN chart_of_accounts coa ON coa.id = jl.account_id
                    WHERE je.restaurant_id = $1 AND je.entry_date = $2
                      AND je.is_reversed = false AND coa.system_code = 'DISCOUNT_EXPENSE'
                      {bc}
                """, *p)

                # Channel breakdown
                channels = await conn.fetch(f"""
                    SELECT je.reference_type, COALESCE(SUM(jl.credit), 0) AS amount
                    FROM journal_lines jl
                    JOIN journal_entries je ON je.id = jl.journal_entry_id
                    JOIN chart_of_accounts coa ON coa.id = jl.account_id
                    WHERE je.restaurant_id = $1 AND je.entry_date = $2
                      AND je.is_reversed = false AND coa.system_code = 'SALES_REVENUE'
                      {bc}
                    GROUP BY je.reference_type
                """, *p)

                orders = await conn.fetchval(f"""
                    SELECT COUNT(DISTINCT je.reference_id)
                    FROM journal_entries je
                    WHERE je.restaurant_id = $1 AND je.entry_date = $2
                      AND je.is_reversed = false AND je.reference_type = 'order'
                      {bc}
                """, *p)

                r = float(Decimal(str(rev)))
                c = float(Decimal(str(cogs)))
                e = float(Decimal(str(expenses)))
                disc = float(Decimal(str(discounts)))
                return {
                    "date": d.isoformat(),
                    "revenue": r,
                    "cogs": c,
                    "expenses": e,
                    "discounts": disc,
                    "gross_profit": r - c,
                    "net_profit": r - c - e,
                    "food_cost_pct": round(c / r * 100, 1) if r > 0 else 0,
                    "order_count": orders or 0,
                    "avg_order_value": round(r / (orders or 1), 2),
                    "channels": {row["reference_type"]: float(row["amount"]) for row in channels},
                }

            today = await _day_breakdown(target_date)
            compare = await _day_breakdown(compare_date)

        # Generate insights
        insights = []
        rev_change = today["revenue"] - compare["revenue"]
        if compare["revenue"] > 0:
            rev_pct = round(rev_change / compare["revenue"] * 100, 1)
        else:
            rev_pct = 0

        if rev_change < 0:
            insights.append(f"Revenue dropped ₹{abs(rev_change):.0f} ({rev_pct}%) vs last week")
            # Check why
            if today["order_count"] < compare["order_count"]:
                insights.append(f"Fewer orders: {today['order_count']} vs {compare['order_count']} last week")
            if today["avg_order_value"] < compare["avg_order_value"]:
                insights.append(f"Lower avg order: ₹{today['avg_order_value']:.0f} vs ₹{compare['avg_order_value']:.0f}")
            # Channel analysis
            for ch, amt in compare["channels"].items():
                cur = today["channels"].get(ch, 0)
                if cur < amt * 0.8:
                    insights.append(f"'{ch}' channel down {round((1 - cur/max(amt,1))*100)}%")
        elif rev_change > 0:
            insights.append(f"Revenue up ₹{rev_change:.0f} ({rev_pct}%) vs last week")

        if today["food_cost_pct"] > compare["food_cost_pct"] + 5:
            insights.append(f"Food cost rose to {today['food_cost_pct']}% (was {compare['food_cost_pct']}%)")
        if today["discounts"] > compare["discounts"] * 1.5 and compare["discounts"] > 0:
            insights.append(f"Discounts jumped to ₹{today['discounts']:.0f} (was ₹{compare['discounts']:.0f})")
        if today["expenses"] > compare["expenses"] * 1.3 and compare["expenses"] > 0:
            insights.append(f"Expenses up to ₹{today['expenses']:.0f} (was ₹{compare['expenses']:.0f})")

        if not insights:
            insights.append("No significant changes detected")

        return {
            "target": today,
            "comparison": compare,
            "revenue_change": rev_change,
            "revenue_change_pct": rev_pct,
            "profit_change": (today["net_profit"] - compare["net_profit"]),
            "insights": insights,
        }

    async def channel_analysis(
        self,
        restaurant_id: str,
        from_date: date,
        to_date: date,
        branch_id: Optional[str] = None,
    ) -> list[dict]:
        """Which channel is making/losing money?"""
        rid = UUID(restaurant_id)
        bid = UUID(branch_id) if branch_id else None
        bc = "AND je.branch_id = $4" if bid else ""
        params: list = [rid, from_date, to_date]
        if bid:
            params.append(bid)

        async with get_connection() as conn:
            rows = await conn.fetch(f"""
                SELECT je.reference_type AS channel,
                       COUNT(DISTINCT je.reference_id) AS order_count,
                       COALESCE(SUM(CASE WHEN coa.system_code = 'SALES_REVENUE'
                                         THEN jl.credit ELSE 0 END), 0) AS revenue,
                       COALESCE(SUM(CASE WHEN coa.system_code IN ('COGS_FOOD', 'COGS_BEVERAGE')
                                         THEN jl.debit ELSE 0 END), 0) AS cogs,
                       COALESCE(SUM(CASE WHEN coa.system_code = 'DISCOUNT_EXPENSE'
                                         THEN jl.debit ELSE 0 END), 0) AS discounts
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND je.entry_date BETWEEN $2 AND $3
                  AND je.is_reversed = false
                  {bc}
                GROUP BY je.reference_type
                ORDER BY revenue DESC
            """, *params)

        result = []
        total_rev = sum(float(r["revenue"]) for r in rows) or 1
        for r in rows:
            rev = float(r["revenue"])
            cogs = float(r["cogs"])
            disc = float(r["discounts"])
            result.append({
                "channel": r["channel"],
                "order_count": r["order_count"],
                "revenue": rev,
                "cogs": cogs,
                "discounts": disc,
                "gross_profit": rev - cogs,
                "margin_pct": round((rev - cogs) / max(rev, 1) * 100, 1),
                "share_pct": round(rev / total_rev * 100, 1),
            })
        return result

    async def cash_mismatch_history(
        self,
        restaurant_id: str,
        *,
        branch_id: Optional[str] = None,
        days: int = 30,
    ) -> dict:
        """Where is cash mismatch? Track pattern over time."""
        rid = UUID(restaurant_id)
        bid = UUID(branch_id) if branch_id else None
        clauses = ["restaurant_id = $1", "status = 'closed'",
                    f"closing_date >= CURRENT_DATE - INTERVAL '{days} days'"]
        params: list = [rid]
        idx = 2
        if bid:
            clauses.append(f"branch_id = ${idx}")
            params.append(bid)
            idx += 1
        where = " AND ".join(clauses)

        async with get_connection() as conn:
            rows = await conn.fetch(f"""
                SELECT closing_date, cash_difference, card_difference, upi_difference,
                       counted_by
                FROM daily_closings
                WHERE {where}
                ORDER BY closing_date DESC
            """, *params)

        entries = []
        total_cash_diff = 0.0
        total_card_diff = 0.0
        mismatch_days = 0
        for r in rows:
            cd = float(r["cash_difference"] or 0)
            crd = float(r["card_difference"] or 0)
            ud = float(r["upi_difference"] or 0)
            total_cash_diff += cd
            total_card_diff += crd
            if abs(cd) > 0.01 or abs(crd) > 0.01 or abs(ud) > 0.01:
                mismatch_days += 1
            entries.append({
                "date": r["closing_date"].isoformat(),
                "cash_difference": cd,
                "card_difference": crd,
                "upi_difference": ud,
                "counted_by": r["counted_by"],
            })

        insights = []
        if mismatch_days > len(entries) * 0.5 and len(entries) > 3:
            insights.append("Cash mismatches are frequent — possible systematic issue")
        if total_cash_diff < -500:
            insights.append(f"Cumulative cash shortage of ₹{abs(total_cash_diff):.0f} — investigate theft or errors")
        if total_cash_diff > 500:
            insights.append(f"Cumulative cash surplus of ₹{total_cash_diff:.0f} — check for unrecorded transactions")

        return {
            "days_analyzed": len(entries),
            "mismatch_days": mismatch_days,
            "total_cash_difference": round(total_cash_diff, 2),
            "total_card_difference": round(total_card_diff, 2),
            "entries": entries,
            "insights": insights,
        }

    # ══════════════════════════════════════════════════════════════════════
    # TRUST STATUS — single call for frontend badges
    # ══════════════════════════════════════════════════════════════════════

    async def trust_status(self, restaurant_id: str) -> dict:
        """
        Single-call system health for frontend badges:
        - ledger_balanced: ✔️ or ✘
        - period_locked: last locked date
        - audit_safe: no integrity violations
        - unresolved_alerts: count
        - last_closing: date + status
        - gst_status: current period status
        """
        rid = UUID(restaurant_id)
        async with get_connection() as conn:
            # Trial balance
            tb = await conn.fetchrow("""
                SELECT COALESCE(SUM(jl.debit), 0) AS d, COALESCE(SUM(jl.credit), 0) AS c
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                WHERE je.restaurant_id = $1
            """, rid)
            balanced = abs(float(tb["d"]) - float(tb["c"])) < 0.01

            # Last locked period
            last_lock = await conn.fetchrow("""
                SELECT period_end, locked_at FROM accounting_periods
                WHERE restaurant_id = $1 AND is_locked = true
                ORDER BY period_end DESC LIMIT 1
            """, rid)

            # Unresolved alerts
            alert_count = await conn.fetchval("""
                SELECT COUNT(*) FROM financial_alerts
                WHERE restaurant_id = $1 AND is_resolved = false
            """, rid)

            # Last closing
            last_close = await conn.fetchrow("""
                SELECT closing_date, status FROM daily_closings
                WHERE restaurant_id = $1
                ORDER BY closing_date DESC LIMIT 1
            """, rid)

            # GST status
            gst = await conn.fetchrow("""
                SELECT period_start, period_end, status FROM gst_filing_workflows
                WHERE restaurant_id = $1
                ORDER BY period_end DESC LIMIT 1
            """, rid)

            # Orphan check (quick integrity)
            orphans = await conn.fetchval("""
                SELECT COUNT(*) FROM journal_lines jl
                LEFT JOIN journal_entries je ON je.id = jl.journal_entry_id
                WHERE je.id IS NULL
            """)

        return {
            "ledger_balanced": balanced,
            "ledger_balanced_icon": "✔️" if balanced else "✘",
            "period_locked": {
                "last_date": last_lock["period_end"].isoformat() if last_lock else None,
                "locked_at": last_lock["locked_at"].isoformat() if last_lock and last_lock["locked_at"] else None,
            } if last_lock else None,
            "period_locked_icon": "🔒" if last_lock else "⚠️",
            "audit_safe": balanced and (orphans or 0) == 0,
            "audit_safe_icon": "✔️" if balanced and (orphans or 0) == 0 else "✘",
            "unresolved_alerts": alert_count or 0,
            "alerts_icon": "✔️" if (alert_count or 0) == 0 else f"⚠️ {alert_count}",
            "last_closing": {
                "date": last_close["closing_date"].isoformat(),
                "status": last_close["status"],
            } if last_close else None,
            "gst_status": {
                "period": f"{gst['period_start'].isoformat()} to {gst['period_end'].isoformat()}",
                "status": gst["status"],
            } if gst else None,
        }

    # ══════════════════════════════════════════════════════════════════════
    # ENHANCED ALERTS — with actions
    # ══════════════════════════════════════════════════════════════════════

    async def resolve_alert_with_notes(
        self,
        restaurant_id: str,
        alert_id: str,
        user_id: str,
        resolution_notes: str,
    ) -> bool:
        """Resolve an alert with explanation of what was done."""
        rid = UUID(restaurant_id)
        aid = UUID(alert_id)
        async with get_connection() as conn:
            tag = await conn.execute("""
                UPDATE financial_alerts
                SET is_resolved = true, resolved_by = $3, resolved_at = NOW(),
                    resolution_notes = $4
                WHERE id = $2 AND restaurant_id = $1 AND is_resolved = false
            """, rid, aid, user_id, resolution_notes)
        return tag == "UPDATE 1"

    async def list_alerts(
        self,
        restaurant_id: str,
        *,
        resolved: Optional[bool] = None,
        alert_type: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List financial alerts with actionability info."""
        rid = UUID(restaurant_id)
        clauses = ["restaurant_id = $1"]
        params: list = [rid]
        idx = 2

        if resolved is not None:
            clauses.append(f"is_resolved = ${idx}")
            params.append(resolved)
            idx += 1
        if alert_type:
            clauses.append(f"alert_type = ${idx}")
            params.append(alert_type)
            idx += 1
        if severity:
            clauses.append(f"severity = ${idx}")
            params.append(severity)
            idx += 1

        where = " AND ".join(clauses)
        params.extend([limit, offset])

        async with get_connection() as conn:
            rows = await conn.fetch(f"""
                SELECT id, restaurant_id, branch_id, alert_type, severity,
                       title, details, suggested_action, is_resolved,
                       resolved_by, resolved_at, resolution_notes,
                       notified, created_at
                FROM financial_alerts
                WHERE {where}
                ORDER BY
                    CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                    created_at DESC
                LIMIT ${idx} OFFSET ${idx + 1}
            """, *params)

        return [
            {
                "id": str(r["id"]),
                "alert_type": r["alert_type"],
                "severity": r["severity"],
                "title": r["title"],
                "details": dict(r["details"]) if r["details"] else {},
                "suggested_action": r["suggested_action"],
                "is_resolved": r["is_resolved"],
                "resolved_by": r["resolved_by"],
                "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
                "resolution_notes": r["resolution_notes"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]

    async def alert_summary(self, restaurant_id: str) -> dict:
        """Quick alert overview for dashboard badges."""
        rid = UUID(restaurant_id)
        async with get_connection() as conn:
            rows = await conn.fetch("""
                SELECT severity, COUNT(*) AS cnt
                FROM financial_alerts
                WHERE restaurant_id = $1 AND is_resolved = false
                GROUP BY severity
            """, rid)
        counts = {r["severity"]: r["cnt"] for r in rows}
        return {
            "total_unresolved": sum(counts.values()),
            "errors": counts.get("error", 0),
            "warnings": counts.get("warning", 0),
            "info": counts.get("info", 0),
            "needs_attention": counts.get("error", 0) > 0,
        }

    # ══════════════════════════════════════════════════════════════════════
    # CA VIEW — export-ready report data
    # ══════════════════════════════════════════════════════════════════════

    async def ca_view(self, restaurant_id: str, period_start: date, period_end: date) -> dict:
        """
        Single call for CA — everything a chartered accountant needs:
        trial balance, P&L summary, GST summary, period status.
        """
        from app.services.accounting_engine import accounting_engine
        from app.services.tax_service import tax_service

        rid = restaurant_id
        trial_balance = await accounting_engine.get_trial_balance(rid, period_end)
        income_stmt = await accounting_engine.get_income_statement(rid, period_start, period_end)

        try:
            gst_data = await tax_service.gst_return_data(rid, period_start, period_end)
        except Exception:
            gst_data = None

        async with get_connection() as conn:
            # Period lock status
            lock = await conn.fetchrow("""
                SELECT is_locked, locked_at, locked_by
                FROM accounting_periods
                WHERE restaurant_id = $1
                  AND period_start <= $2 AND period_end >= $3
                LIMIT 1
            """, UUID(rid), period_end, period_start)

            # Journal entry count for audit
            je_count = await conn.fetchval("""
                SELECT COUNT(*) FROM journal_entries
                WHERE restaurant_id = $1
                  AND entry_date BETWEEN $2 AND $3
                  AND is_reversed = false
            """, UUID(rid), period_start, period_end)

            # Reversal count
            rev_count = await conn.fetchval("""
                SELECT COUNT(*) FROM journal_entries
                WHERE restaurant_id = $1
                  AND entry_date BETWEEN $2 AND $3
                  AND is_reversed = true
            """, UUID(rid), period_start, period_end)

        return {
            "period": {"start": period_start.isoformat(), "end": period_end.isoformat()},
            "trial_balance": trial_balance,
            "income_statement": income_stmt,
            "gst_summary": gst_data,
            "period_status": {
                "locked": lock["is_locked"] if lock else False,
                "locked_at": lock["locked_at"].isoformat() if lock and lock["locked_at"] else None,
            },
            "audit_metrics": {
                "journal_entries": je_count or 0,
                "reversals": rev_count or 0,
                "reversal_rate_pct": round((rev_count or 0) / max(je_count or 1, 1) * 100, 2),
            },
        }


finance_service = FinanceService()
