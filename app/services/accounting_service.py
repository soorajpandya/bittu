"""
Production-grade Chart of Accounts + Double Entry Accounting Service.

Architecture
───────────────────────────────────────────────────────────────────────────────
Every financial event writes two tables:
  1. journal_entries  — transaction header (reference_type, reference_id)
  2. journal_lines    — balanced debit/credit lines (account_id FK → CoA)

For backward compatibility, accounting_entries is also written with:
  journal_entry_id, entry_side, account_id (new columns from migration 010/011).

Legacy rows in accounting_entries (entry_side IS NULL) still work in all reads.

P&L and Ledger queries use journal_lines + chart_of_accounts exclusively;
legacy aggregates fall back to accounting_entries for older data.

System accounts are resolved via chart_of_accounts.system_code — never
hardcoded IDs, never frontend-supplied account IDs.
"""
from datetime import date
from decimal import Decimal
from typing import Optional
from uuid import UUID

from app.core.database import get_connection, get_transaction
from app.core.exceptions import ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

ENTRY_TYPES = {"revenue", "expense", "refund"}

# Maps semantic code → (account_code fallback, name search hint)
SYSTEM_ACCOUNT_MAP = {
    "CASH_ACCOUNT": ("1001", "cash"),
    "UPI_ACCOUNT":  ("1002", "upi"),
    "CARD_ACCOUNT": ("1003", "card"),
    "FOOD_SALES":   ("4001", "food sales"),
}


# ── Private helpers ────────────────────────────────────────────────────────────

def _parse_uuid(value) -> Optional[UUID]:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def _to_uuid(value) -> Optional[UUID]:
    return _parse_uuid(value)


def _ensure_balanced(entries: list[dict]):
    debit_total  = sum(Decimal(str(e["amount"])) for e in entries if e["side"] == "debit")
    credit_total = sum(Decimal(str(e["amount"])) for e in entries if e["side"] == "credit")
    if debit_total != credit_total:
        raise ValidationError(
            f"Unbalanced journal entry: debit={debit_total}, credit={credit_total}"
        )


# ── Public account resolver ────────────────────────────────────────────────────

async def get_account_by_code(conn, restaurant_id, system_code: str) -> dict:
    """
    Fetch a CoA account by its system_code for a restaurant.

    Resolution order:
      1. system_code column (canonical, requires migration 011)
      2. account_code column (fallback for older data)
      3. name ILIKE search (last resort)

    Raises ValidationError if not found.
    """
    restaurant_uuid = _parse_uuid(restaurant_id)
    if not restaurant_uuid:
        raise ValidationError(f"Invalid restaurant_id: {restaurant_id!r}")

    cfg = SYSTEM_ACCOUNT_MAP.get(system_code)
    if not cfg:
        raise ValidationError(f"Unknown system_code: {system_code!r}")

    account_code_fallback, name_hint = cfg

    # 1. system_code column
    row = await conn.fetchrow(
        """
        SELECT id, account_code, name, account_type, system_code
          FROM chart_of_accounts
         WHERE restaurant_id = $1
           AND system_code = $2
           AND is_active = true
         LIMIT 1
        """,
        restaurant_uuid, system_code,
    )
    if row:
        return dict(row)

    # 2. account_code fallback
    row = await conn.fetchrow(
        """
        SELECT id, account_code, name, account_type, system_code
          FROM chart_of_accounts
         WHERE restaurant_id = $1
           AND account_code = $2
           AND is_active = true
         LIMIT 1
        """,
        restaurant_uuid, account_code_fallback,
    )
    if row:
        return dict(row)

    # 3. name hint search
    row = await conn.fetchrow(
        """
        SELECT id, account_code, name, account_type, system_code
          FROM chart_of_accounts
         WHERE restaurant_id = $1
           AND is_active = true
           AND lower(name) LIKE $2
         ORDER BY created_at ASC
         LIMIT 1
        """,
        restaurant_uuid, f"%{name_hint}%",
    )
    if row:
        return dict(row)

    raise ValidationError(
        f"System account '{system_code}' not found for restaurant {restaurant_id}. "
        "Run migration 011 or seed chart_of_accounts."
    )


# Keep old name as alias for callers from migration 010 era.
async def get_account(conn, restaurant_id, system_code: str) -> Optional[dict]:
    """Non-raising wrapper around get_account_by_code. Returns None on failure."""
    try:
        return await get_account_by_code(conn, restaurant_id, system_code)
    except ValidationError:
        return None


# ── Journal helpers ────────────────────────────────────────────────────────────

async def create_journal_entry(
    conn,
    restaurant_id,
    reference_type: str,
    reference_id: str,
    description: str = "",
) -> Optional[str]:
    """
    Insert a journal_entries header row and return its UUID string.
    Backward compatible across schema versions.
    """
    restaurant_uuid = _parse_uuid(restaurant_id)
    if not restaurant_uuid:
        return None

    try:
        journal_id = await conn.fetchval(
            """
            INSERT INTO journal_entries (
                restaurant_id, branch_id, entry_date,
                reference_type, reference_id, description, created_by
            ) VALUES ($1, NULL, CURRENT_DATE, $2, $3, $4, 'system')
            RETURNING id
            """,
            restaurant_uuid, reference_type, reference_id, description,
        )
    except Exception:
        # Minimal schema fallback (migration 010 only).
        journal_id = await conn.fetchval(
            """
            INSERT INTO journal_entries (restaurant_id, reference_type, reference_id, description)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            restaurant_uuid, reference_type, reference_id, description,
        )

    return str(journal_id) if journal_id else None


async def _insert_journal_line(
    conn,
    journal_id: str,
    account_id,
    amount: Decimal,
    side: str,
    description: str = "",
):
    """Write one row to journal_lines (the canonical double-entry ledger)."""
    debit  = float(amount) if side == "debit"  else 0.0
    credit = float(amount) if side == "credit" else 0.0
    await conn.execute(
        """
        INSERT INTO journal_lines (journal_entry_id, account_id, debit, credit, description)
        VALUES ($1, $2, $3, $4, $5)
        """,
        _parse_uuid(journal_id),
        _parse_uuid(account_id),
        debit,
        credit,
        description,
    )


async def add_entry(
    conn,
    journal_id,
    account_id,
    amount,
    side: str,
    user_id,
    restaurant_id,
    reference_type: str = "order",
    reference_id=None,
    description: str = "",
):
    """
    Write one double-entry row into accounting_entries (legacy bridge table).
    Also writes the canonical row into journal_lines if account_id is provided.

    The journal_lines insert is the source of truth for P&L / ledger queries.
    The accounting_entries insert ensures backward compat with existing reports.
    """
    side = (side or "").lower()
    if side not in {"debit", "credit"}:
        raise ValidationError("Entry side must be 'debit' or 'credit'")

    amt = Decimal(str(amount or 0))
    if amt <= 0:
        raise ValidationError("Entry amount must be > 0")

    # ── journal_lines (canonical) ──
    if account_id and journal_id:
        await _insert_journal_line(conn, journal_id, account_id, amt, side, description)

    # ── accounting_entries (legacy bridge) ──
    await conn.execute(
        """
        INSERT INTO accounting_entries (
            user_id, restaurant_id, entry_type, amount,
            category, reference_type, reference_id, description,
            journal_entry_id, entry_side, account_id
        ) VALUES (
            $1, $2, 'revenue', $3,
            $4, $5, $6, $7,
            $8, $9, $10
        )
        """,
        str(user_id or "system"),
        str(restaurant_id) if restaurant_id else None,
        float(amt),
        f"double_entry:{account_id}",
        reference_type,
        str(reference_id) if reference_id else (str(journal_id) if journal_id else None),
        description or f"{side} entry",
        _parse_uuid(journal_id),
        side,
        _parse_uuid(account_id),
    )


# ── Core posting function ──────────────────────────────────────────────────────

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
        """
        Record balanced double-entry for a completed cash/UPI/card sale.

        Writes:
          - 1 journal_entries header
          - 2 journal_lines  (debit payment account, credit FOOD_SALES)
          - 2 accounting_entries (legacy bridge with journal_entry_id + entry_side)

        Returns journal_entry_id on success, None if not applicable.
        Raises ValidationError if accounts are missing or entries are unbalanced.
        """
        if not restaurant_id or not order_id:
            return None

        amt = Decimal(str(amount or 0))
        if amt <= 0:
            return None

        async with get_transaction() as conn:
            payment_account = await get_account_by_code(conn, restaurant_id, payment_system_code)
            revenue_account = await get_account_by_code(conn, restaurant_id, "FOOD_SALES")

            journal_id = await create_journal_entry(
                conn,
                restaurant_id=restaurant_id,
                reference_type="order",
                reference_id=order_id,
                description=f"POS sale — order {order_id}",
            )
            if not journal_id:
                return None

            entries = [
                {
                    "account_id": payment_account["id"],
                    "side": "debit",
                    "amount": amt,
                    "description": f"Order {order_id} — cash/payment received",
                },
                {
                    "account_id": revenue_account["id"],
                    "side": "credit",
                    "amount": amt,
                    "description": f"Order {order_id} — food sales revenue",
                },
            ]
            _ensure_balanced(entries)

            for e in entries:
                await add_entry(
                    conn,
                    journal_id=journal_id,
                    account_id=e["account_id"],
                    amount=float(e["amount"]),
                    side=e["side"],
                    user_id=user_id,
                    restaurant_id=restaurant_id,
                    reference_type="order",
                    reference_id=order_id,
                    description=e["description"],
                )

        return journal_id

    # ── Ledger ─────────────────────────────────────────────────────────────────

    async def get_ledger(
        self,
        account_id: str,
        from_date: date,
        to_date: date,
        restaurant_id: Optional[str] = None,
    ) -> dict:
        """
        T-account ledger for a single CoA account.

        Returns every journal_lines movement within the date range plus
        running balance, opening balance, and closing balance.

        Debits increase assets/expenses; credits increase liabilities/revenue.
        """
        account_uuid = _parse_uuid(account_id)
        if not account_uuid:
            raise ValidationError(f"Invalid account_id: {account_id!r}")

        async with get_connection() as conn:
            account = await conn.fetchrow(
                "SELECT id, name, account_type, account_code FROM chart_of_accounts WHERE id = $1",
                account_uuid,
            )
            if not account:
                raise ValidationError(f"Account {account_id} not found")

            # Opening balance: all movements BEFORE from_date
            opening_row = await conn.fetchrow(
                """
                SELECT
                    COALESCE(SUM(jl.debit),  0) AS total_debit,
                    COALESCE(SUM(jl.credit), 0) AS total_credit
                  FROM journal_lines jl
                  JOIN journal_entries je ON je.id = jl.journal_entry_id
                 WHERE jl.account_id = $1
                   AND je.entry_date < $2
                   AND ($3::uuid IS NULL OR je.restaurant_id = $3)
                """,
                account_uuid, from_date, _parse_uuid(restaurant_id),
            )

            lines = await conn.fetch(
                """
                SELECT
                    jl.id,
                    je.entry_date,
                    je.reference_type,
                    je.reference_id,
                    je.description       AS journal_description,
                    jl.description       AS line_description,
                    jl.debit,
                    jl.credit,
                    je.id                AS journal_entry_id
                  FROM journal_lines jl
                  JOIN journal_entries je ON je.id = jl.journal_entry_id
                 WHERE jl.account_id = $1
                   AND je.entry_date BETWEEN $2 AND $3
                   AND ($4::uuid IS NULL OR je.restaurant_id = $4)
                 ORDER BY je.entry_date, je.created_at
                """,
                account_uuid, from_date, to_date, _parse_uuid(restaurant_id),
            )

        account_type = account["account_type"]
        # Normal balance: assets/expenses debit-normal; liabilities/revenue credit-normal
        debit_normal = account_type in ("asset", "expense")

        opening_debit  = Decimal(str(opening_row["total_debit"]))
        opening_credit = Decimal(str(opening_row["total_credit"]))
        opening_balance = opening_debit - opening_credit if debit_normal else opening_credit - opening_debit

        running = opening_balance
        formatted = []
        for r in lines:
            debit  = Decimal(str(r["debit"]))
            credit = Decimal(str(r["credit"]))
            movement = debit - credit if debit_normal else credit - debit
            running += movement
            formatted.append({
                "entry_date":          r["entry_date"].isoformat(),
                "reference_type":      r["reference_type"],
                "reference_id":        r["reference_id"],
                "description":         r["line_description"] or r["journal_description"],
                "debit":               float(debit),
                "credit":              float(credit),
                "running_balance":     float(running),
                "journal_entry_id":    str(r["journal_entry_id"]),
            })

        return {
            "account": {
                "id":           str(account["id"]),
                "code":         account["account_code"],
                "name":         account["name"],
                "account_type": account_type,
            },
            "period":           {"from": from_date.isoformat(), "to": to_date.isoformat()},
            "opening_balance":  float(opening_balance),
            "closing_balance":  float(running),
            "lines":            formatted,
        }

    # ── P&L ────────────────────────────────────────────────────────────────────

    async def get_pnl(
        self,
        restaurant_id: str,
        from_date: date,
        to_date: date,
    ) -> dict:
        """
        Income Statement (P&L) for a restaurant over a date range.

        Revenue  = SUM credit entries on revenue-type accounts
        Expenses = SUM debit  entries on expense-type accounts
        Profit   = Revenue - Expenses

        Includes itemised breakdown by account.
        """
        restaurant_uuid = _parse_uuid(restaurant_id)
        if not restaurant_uuid:
            raise ValidationError(f"Invalid restaurant_id: {restaurant_id!r}")

        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    coa.id,
                    coa.name,
                    coa.account_type,
                    coa.account_code,
                    COALESCE(SUM(jl.credit), 0) AS total_credit,
                    COALESCE(SUM(jl.debit),  0) AS total_debit
                  FROM journal_lines jl
                  JOIN journal_entries je ON je.id = jl.journal_entry_id
                  JOIN chart_of_accounts coa ON coa.id = jl.account_id
                 WHERE je.restaurant_id = $1
                   AND je.entry_date BETWEEN $2 AND $3
                   AND coa.account_type IN ('revenue', 'expense')
                 GROUP BY coa.id, coa.name, coa.account_type, coa.account_code
                 ORDER BY coa.account_type DESC, coa.account_code
                """,
                restaurant_uuid, from_date, to_date,
            )

        revenue_lines = []
        expense_lines = []
        total_revenue  = Decimal("0")
        total_expenses = Decimal("0")

        for r in rows:
            if r["account_type"] == "revenue":
                # Credit-normal: revenue recognised on credits
                amount = Decimal(str(r["total_credit"])) - Decimal(str(r["total_debit"]))
                total_revenue += amount
                revenue_lines.append({
                    "account_id":   str(r["id"]),
                    "account_code": r["account_code"],
                    "name":         r["name"],
                    "amount":       float(amount),
                })
            elif r["account_type"] == "expense":
                # Debit-normal: expenses recognised on debits
                amount = Decimal(str(r["total_debit"])) - Decimal(str(r["total_credit"]))
                total_expenses += amount
                expense_lines.append({
                    "account_id":   str(r["id"]),
                    "account_code": r["account_code"],
                    "name":         r["name"],
                    "amount":       float(amount),
                })

        profit = total_revenue - total_expenses

        return {
            "period":          {"from": from_date.isoformat(), "to": to_date.isoformat()},
            "total_revenue":   float(total_revenue),
            "total_expenses":  float(total_expenses),
            "profit":          float(profit),
            "revenue_accounts": revenue_lines,
            "expense_accounts": expense_lines,
        }

    # ── Legacy event handlers (unchanged public API) ───────────────────────────

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

    # ── Cash flow / legacy report APIs (backward compat) ──────────────────────

    async def get_cash_flow(
        self,
        user_id: str,
        branch_id: Optional[str],
        start_date: date,
        end_date: date,
    ) -> dict:
        """
        Revenue vs expenses summary for a date range.
        Merges legacy accounting_entries rows with modern journal_lines rows.
        """
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
                        COALESCE(SUM(jl.credit) FILTER (WHERE coa.account_type = 'revenue'), 0) AS modern_revenue,
                        COALESCE(SUM(jl.debit)  FILTER (WHERE coa.account_type = 'expense'), 0) AS modern_expenses
                    FROM journal_lines jl
                    JOIN journal_entries je ON je.id = jl.journal_entry_id
                    JOIN chart_of_accounts coa ON coa.id = jl.account_id
                    -- scope to the user's restaurants
                    JOIN restaurants r ON r.id = je.restaurant_id
                    WHERE DATE(je.entry_date) BETWEEN $3 AND $4
                      AND ($2::uuid IS NULL OR je.branch_id = $2)
                      -- only count entries NOT already in legacy to avoid double-counting
                      AND NOT EXISTS (
                        SELECT 1 FROM accounting_entries ae
                        WHERE ae.journal_entry_id = je.id
                          AND ae.entry_side IS NOT NULL
                          AND ae.user_id = $1
                      )
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
            "total_revenue":  float(row["total_revenue"]),
            "total_expenses": float(row["total_expenses"]),
            "total_refunds":  float(row["total_refunds"]),
            "net_cash_flow":  float(row["net_cash_flow"]),
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
        sql += f" ORDER BY created_at DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}"

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
        """Revenue and expenses grouped by day (legacy accounting_entries)."""
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    DATE(created_at) AS day,
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
                "date":     r["day"].isoformat(),
                "revenue":  float(r["revenue"]),
                "expenses": float(r["expenses"]),
                "refunds":  float(r["refunds"]),
                "net":      float(r["net"]),
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
        """Revenue split by payment method (legacy accounting_entries)."""
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
