"""
Lightweight Accounting Service — revenue, expenses, cash flow.

NOT a full Tally clone. Tracks:
  - Revenue entries (from payment.completed events)
  - Expense entries (manual or from purchase orders)
  - Refund entries (from payment.refunded events)
  - Cash flow summary (revenue - expenses for a period)
"""
from datetime import date, datetime, timezone
from typing import Optional
from decimal import Decimal

from app.core.database import get_connection, get_transaction
from app.core.logging import get_logger

logger = get_logger(__name__)

ENTRY_TYPES = {"revenue", "expense", "refund"}


class AccountingService:

    # ── Auto-recorded from events ──

    async def record_revenue(
        self,
        user_id: str,
        restaurant_id: Optional[str],
        branch_id: Optional[str],
        order_id: str,
        payment_id: Optional[str],
        amount: float,
        method: str = "unknown",
    ):
        """Called by ERP event handler on PAYMENT_COMPLETED."""
        async with get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO accounting_entries
                    (user_id, restaurant_id, branch_id, entry_type, amount,
                     payment_method, reference_type, reference_id, description)
                VALUES ($1, $2, $3, 'revenue', $4, $5, 'order', $6, $7)
                """,
                user_id, restaurant_id, branch_id, amount, method,
                order_id, f"Payment {payment_id or ''} for order {order_id}",
            )

    async def record_refund(
        self,
        user_id: str,
        restaurant_id: Optional[str],
        branch_id: Optional[str],
        order_id: str,
        payment_id: Optional[str],
        amount: float,
    ):
        """Called by ERP event handler on PAYMENT_REFUNDED."""
        async with get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO accounting_entries
                    (user_id, restaurant_id, branch_id, entry_type, amount,
                     payment_method, reference_type, reference_id, description)
                VALUES ($1, $2, $3, 'refund', $4, 'refund', 'order', $5, $6)
                """,
                user_id, restaurant_id, branch_id, -abs(amount),
                order_id, f"Refund for order {order_id}",
            )

    async def record_expense(
        self,
        user_id: str,
        restaurant_id: Optional[str],
        branch_id: Optional[str],
        amount: float,
        category: str,
        description: str = "",
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
    ):
        """Record a manual or PO-based expense."""
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO accounting_entries
                    (user_id, restaurant_id, branch_id, entry_type, amount,
                     category, reference_type, reference_id, description)
                VALUES ($1, $2, $3, 'expense', $4, $5, $6, $7, $8)
                RETURNING *
                """,
                user_id, restaurant_id, branch_id, -abs(amount),
                category, reference_type, reference_id, description,
            )
        return dict(row)

    # ── Query APIs ──

    async def get_cash_flow(
        self,
        user_id: str,
        branch_id: Optional[str],
        start_date: date,
        end_date: date,
    ) -> dict:
        """Revenue vs expenses summary for a date range."""
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COALESCE(SUM(amount) FILTER (WHERE entry_type = 'revenue'), 0) AS total_revenue,
                    COALESCE(SUM(ABS(amount)) FILTER (WHERE entry_type = 'expense'), 0) AS total_expenses,
                    COALESCE(SUM(ABS(amount)) FILTER (WHERE entry_type = 'refund'), 0) AS total_refunds,
                    COALESCE(SUM(amount), 0) AS net_cash_flow
                FROM accounting_entries
                WHERE user_id = $1
                  AND ($2::uuid IS NULL OR branch_id = $2)
                  AND DATE(created_at) BETWEEN $3 AND $4
                """,
                user_id, branch_id, start_date, end_date,
            )
        return {
            "total_revenue": float(row["total_revenue"]),
            "total_expenses": float(row["total_expenses"]),
            "total_refunds": float(row["total_refunds"]),
            "net_cash_flow": float(row["net_cash_flow"]),
            "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        }

    async def get_entries(
        self,
        user_id: str,
        branch_id: Optional[str],
        entry_type: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List accounting entries with optional filters."""
        sql = "SELECT * FROM accounting_entries WHERE user_id = $1"
        params: list = [user_id]

        if branch_id:
            params.append(branch_id)
            sql += f" AND branch_id = ${len(params)}"
        if entry_type and entry_type in ENTRY_TYPES:
            params.append(entry_type)
            sql += f" AND entry_type = ${len(params)}"
        if start_date:
            params.append(start_date)
            sql += f" AND DATE(created_at) >= ${len(params)}"
        if end_date:
            params.append(end_date)
            sql += f" AND DATE(created_at) <= ${len(params)}"

        params.extend([limit, offset])
        sql += f" ORDER BY created_at DESC LIMIT ${len(params)-1} OFFSET ${len(params)}"

        async with get_connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    async def get_daily_breakdown(
        self,
        user_id: str,
        branch_id: Optional[str],
        start_date: date,
        end_date: date,
    ) -> list[dict]:
        """Revenue and expenses grouped by day."""
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    DATE(created_at) AS date,
                    COALESCE(SUM(amount) FILTER (WHERE entry_type = 'revenue'), 0) AS revenue,
                    COALESCE(SUM(ABS(amount)) FILTER (WHERE entry_type = 'expense'), 0) AS expenses,
                    COALESCE(SUM(ABS(amount)) FILTER (WHERE entry_type = 'refund'), 0) AS refunds,
                    COALESCE(SUM(amount), 0) AS net
                FROM accounting_entries
                WHERE user_id = $1
                  AND ($2::uuid IS NULL OR branch_id = $2)
                  AND DATE(created_at) BETWEEN $3 AND $4
                GROUP BY DATE(created_at)
                ORDER BY DATE(created_at)
                """,
                user_id, branch_id, start_date, end_date,
            )
        return [
            {
                "date": r["date"].isoformat(),
                "revenue": float(r["revenue"]),
                "expenses": float(r["expenses"]),
                "refunds": float(r["refunds"]),
                "net": float(r["net"]),
            }
            for r in rows
        ]

    async def get_payment_method_breakdown(
        self,
        user_id: str,
        branch_id: Optional[str],
        start_date: date,
        end_date: date,
    ) -> list[dict]:
        """Revenue split by payment method."""
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    COALESCE(payment_method, 'unknown') AS method,
                    SUM(amount) AS total
                FROM accounting_entries
                WHERE user_id = $1
                  AND entry_type = 'revenue'
                  AND ($2::uuid IS NULL OR branch_id = $2)
                  AND DATE(created_at) BETWEEN $3 AND $4
                GROUP BY payment_method
                ORDER BY total DESC
                """,
                user_id, branch_id, start_date, end_date,
            )
        return [{"method": r["method"], "total": float(r["total"])} for r in rows]
