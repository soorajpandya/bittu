"""
Finance Dashboard & Alerts Service
───────────────────────────────────
Provides the Financial Operating System's dashboard metrics,
alert scanning, materialized-view management, and audit logging.

All numbers are derived from the double-entry ledger (journal_entries +
journal_lines).  Materialized views (mv_daily_revenue, mv_account_balances)
are used for fast dashboard reads and refreshed on-demand.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from app.core.database import get_connection


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


finance_service = FinanceService()
