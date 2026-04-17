"""
Lightweight Accounting Service — revenue, expenses, cash flow.

NOT a full Tally clone. Tracks:
  - Revenue entries (from payment.completed events)
  - Expense entries (manual or from purchase orders)
  - Refund entries (from payment.refunded events)
  - Cash flow summary (revenue - expenses for a period)
"""
from datetime import date
from typing import Optional
from decimal import Decimal
from uuid import UUID

from app.core.database import get_connection, get_transaction
from app.core.logging import get_logger
from app.core.exceptions import ValidationError

logger = get_logger(__name__)

ENTRY_TYPES = {"revenue", "expense", "refund"}

SYSTEM_ACCOUNT_MAP = {
    "FOOD_SALES": {
        "account_code": "4001",
        "name_hint": "sales",
    },
    "CASH_ACCOUNT": {
        "account_code": "1001",
        "name_hint": "cash",
    },
    "UPI_ACCOUNT": {
        "account_code": "1002",
        "name_hint": "bank",
    },
}


def _parse_uuid_or_none(value: Optional[str]) -> Optional[UUID]:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


async def get_account(db, restaurant_id, system_code):
    """
    Fetch account from chart_of_accounts by system code.

    Supported system_code examples:
    - FOOD_SALES
    - CASH_ACCOUNT
    - UPI_ACCOUNT
    """
    cfg = SYSTEM_ACCOUNT_MAP.get(system_code)
    if not cfg:
        return None

    restaurant_uuid = _parse_uuid_or_none(restaurant_id)
    if not restaurant_uuid:
        return None

    row = await db.fetchrow(
        """
        SELECT id, account_code, name, account_type
          FROM chart_of_accounts
         WHERE restaurant_id = $1
           AND account_code = $2
           AND is_active = true
         LIMIT 1
        """,
        restaurant_uuid,
        cfg["account_code"],
    )
    if row:
        return dict(row)

    row = await db.fetchrow(
        """
        SELECT id, account_code, name, account_type
          FROM chart_of_accounts
         WHERE restaurant_id = $1
           AND is_active = true
           AND lower(name) LIKE $2
         ORDER BY created_at ASC
         LIMIT 1
        """,
        restaurant_uuid,
        f"%{cfg['name_hint']}%",
    )
    return dict(row) if row else None


async def create_journal_entry(db, restaurant_id, reference_type, reference_id, description=""):
    """Create a journal header in a backward-compatible way across schema versions."""
    restaurant_uuid = _parse_uuid_or_none(restaurant_id)
    if not restaurant_uuid:
        return None

    try:
        # Preferred insert for richer schema (migration 006+).
        journal_id = await db.fetchval(
            """
            INSERT INTO journal_entries (
                restaurant_id, branch_id, entry_date, reference_type,
                reference_id, description, created_by
            )
            VALUES ($1, NULL, CURRENT_DATE, $2, $3, $4, 'system')
            RETURNING id
            """,
            restaurant_uuid,
            reference_type,
            reference_id,
            description,
        )
    except Exception:
        # Fallback for minimal table shape.
        journal_id = await db.fetchval(
            """
            INSERT INTO journal_entries (
                restaurant_id, reference_type, reference_id, description
            )
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            restaurant_uuid,
            reference_type,
            reference_id,
            description,
        )

    return str(journal_id) if journal_id else None


async def add_entry(
    db,
    journal_id,
    account_id,
    amount,
    side,
    user_id,
    restaurant_id,
    reference_type="order",
    reference_id=None,
    description=None,
):
    """Insert one double-entry line into legacy accounting_entries (extended schema)."""
    normalized_side = (side or "").lower()
    if normalized_side not in {"debit", "credit"}:
        raise ValidationError("Entry side must be either 'debit' or 'credit'")

    amt = Decimal(str(amount or 0))
    if amt <= 0:
        raise ValidationError("Entry amount must be greater than zero")

    await db.execute(
        """
        INSERT INTO accounting_entries (
            user_id,
            restaurant_id,
            entry_type,
            amount,
            payment_method,
            category,
            reference_type,
            reference_id,
            description,
            journal_entry_id,
            entry_side
        ) VALUES (
            $1, $2, 'revenue', $3,
            NULL, $4, $5, $6,
            $7, $8, $9
        )
        """,
        str(user_id or "system"),
        str(restaurant_id) if restaurant_id else None,
        float(amt),
        f"double_entry:{account_id}",
        reference_type,
        str(reference_id) if reference_id else str(journal_id) if journal_id else None,
        description or f"{normalized_side} entry",
        str(journal_id) if journal_id else None,
        normalized_side,
    )


def _ensure_balanced(entries: list[dict]):
    debit_total = Decimal("0")
    credit_total = Decimal("0")
    for entry in entries:
        side = (entry.get("side") or "").lower()
        amt = Decimal(str(entry.get("amount") or 0))
        if side == "debit":
            debit_total += amt
        elif side == "credit":
            credit_total += amt

    if debit_total != credit_total:
        raise ValidationError(
            f"Unbalanced accounting entries: debit={debit_total}, credit={credit_total}"
        )


class AccountingService:

    async def record_order_sale_double_entry(
        self,
        *,
        user_id: str,
        restaurant_id: Optional[str],
        order_id: str,
        amount: float,
        payment_system_code: str = "CASH_ACCOUNT",
    ) -> Optional[str]:
        """Record balanced double-entry rows for a completed cash sale order."""
        if not restaurant_id or not order_id:
            return None

        amt = Decimal(str(amount or 0))
        if amt <= 0:
            return None

        async with get_transaction() as conn:
            payment_account = await get_account(conn, restaurant_id, payment_system_code)
            revenue_account = await get_account(conn, restaurant_id, "FOOD_SALES")

            if not payment_account or not revenue_account:
                raise ValidationError("Required chart_of_accounts system accounts are missing")

            journal_id = await create_journal_entry(
                conn,
                restaurant_id=restaurant_id,
                reference_type="order",
                reference_id=order_id,
                description=f"POS sale for order {order_id}",
            )

            if not journal_id:
                return None

            entries = [
                {
                    "account_id": payment_account["id"],
                    "amount": amt,
                    "side": "debit",
                },
                {
                    "account_id": revenue_account["id"],
                    "amount": amt,
                    "side": "credit",
                },
            ]
            _ensure_balanced(entries)

            for entry in entries:
                await add_entry(
                    conn,
                    journal_id=journal_id,
                    account_id=entry["account_id"],
                    amount=float(entry["amount"]),
                    side=entry["side"],
                    user_id=user_id,
                    restaurant_id=restaurant_id,
                    reference_type="order",
                    reference_id=order_id,
                    description=f"Order {order_id} {entry['side']}",
                )

            return journal_id

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
                WITH legacy AS (
                    SELECT
                        COALESCE(SUM(amount) FILTER (WHERE entry_type = 'revenue' AND entry_side IS NULL), 0) AS legacy_revenue,
                        COALESCE(SUM(ABS(amount)) FILTER (WHERE entry_type = 'expense' AND entry_side IS NULL), 0) AS legacy_expenses,
                        COALESCE(SUM(ABS(amount)) FILTER (WHERE entry_type = 'refund' AND entry_side IS NULL), 0) AS legacy_refunds
                    FROM accounting_entries
                    WHERE user_id = $1
                      AND ($2::uuid IS NULL OR branch_id = $2)
                      AND DATE(created_at) BETWEEN $3 AND $4
                ),
                modern AS (
                    SELECT
                        COALESCE(
                            SUM(ae.amount) FILTER (
                                WHERE ae.entry_side = 'credit' AND coa.account_type = 'revenue'
                            ),
                            0
                        ) AS modern_revenue,
                        COALESCE(
                            SUM(ae.amount) FILTER (
                                WHERE ae.entry_side = 'debit' AND coa.account_type = 'expense'
                            ),
                            0
                        ) AS modern_expenses
                    FROM accounting_entries ae
                    LEFT JOIN journal_lines jl ON jl.journal_entry_id = ae.journal_entry_id
                    LEFT JOIN chart_of_accounts coa ON coa.id = jl.account_id
                    WHERE ae.user_id = $1
                      AND ($2::uuid IS NULL OR ae.branch_id = $2)
                      AND DATE(ae.created_at) BETWEEN $3 AND $4
                      AND ae.entry_side IS NOT NULL
                )
                SELECT
                    legacy.legacy_revenue + modern.modern_revenue AS total_revenue,
                    legacy.legacy_expenses + modern.modern_expenses AS total_expenses,
                    legacy.legacy_refunds AS total_refunds,
                    (legacy.legacy_revenue + modern.modern_revenue)
                      - (legacy.legacy_expenses + modern.modern_expenses)
                      - legacy.legacy_refunds AS net_cash_flow
                FROM legacy, modern
                """,
                user_id,
                branch_id,
                start_date,
                end_date,
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
