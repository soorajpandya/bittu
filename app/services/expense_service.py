"""
Expense Service — Structured expense management.

Categories, approval, recurring flags, journal linking.

Usage:
    from app.services.expense_service import expense_service
    exp = await expense_service.create_expense(...)
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from uuid import UUID

from app.core.database import get_connection, get_serializable_transaction
from app.core.exceptions import ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)


def _q(val) -> Decimal:
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# Maps category account_codes to system account names
EXPENSE_ACCOUNT_MAP = {
    "5001": "COGS_FOOD",
    "5002": "COGS_BEVERAGE",
    "5011": "GATEWAY_CHARGES",
    "5020": "RENT_EXPENSE",
    "5021": "SALARY_EXPENSE",
    "5022": "UTILITIES_EXPENSE",
    "5030": "MISC_EXPENSE",
}


class ExpenseService:

    # ── Category CRUD ────────────────────────────────────────────────────────

    async def list_categories(
        self, restaurant_id: str, active_only: bool = True,
    ) -> list[dict]:
        q = "SELECT * FROM expense_categories WHERE restaurant_id = $1"
        if active_only:
            q += " AND is_active = true"
        q += " ORDER BY name"

        async with get_connection() as conn:
            rows = await conn.fetch(q, UUID(restaurant_id))
        return [dict(r) for r in rows]

    async def create_category(
        self,
        restaurant_id: str,
        name: str,
        account_code: str,
        description: str = "",
    ) -> dict:
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """INSERT INTO expense_categories
                    (restaurant_id, name, account_code, description)
                VALUES ($1, $2, $3, $4)
                RETURNING *""",
                UUID(restaurant_id), name, account_code, description,
            )
        return dict(row)

    # ── Expense lifecycle ────────────────────────────────────────────────────

    async def create_expense(
        self,
        *,
        restaurant_id: str,
        branch_id: Optional[str] = None,
        category_id: Optional[str] = None,
        category_name: Optional[str] = None,
        vendor_id: Optional[str] = None,
        vendor_name: Optional[str] = None,
        amount: float,
        tax_amount: float = 0,
        payment_method: str = "cash",
        payment_status: str = "paid",
        expense_date: Optional[date] = None,
        description: str = "",
        receipt_url: Optional[str] = None,
        invoice_number: Optional[str] = None,
        is_recurring: bool = False,
        recurrence: Optional[str] = None,
        created_by: str = "system",
    ) -> dict:
        """Create an expense and post journal entry."""
        amt = _q(amount)
        tax = _q(tax_amount)
        total = _q(amt + tax)

        if amt <= 0:
            raise ValidationError("Expense amount must be > 0")

        # Resolve expense account from category
        expense_account = "MISC_EXPENSE"
        if category_id:
            async with get_connection() as conn:
                cat = await conn.fetchrow(
                    "SELECT name, account_code FROM expense_categories WHERE id = $1",
                    UUID(category_id),
                )
                if cat:
                    category_name = cat["name"]
                    expense_account = EXPENSE_ACCOUNT_MAP.get(
                        cat["account_code"], "MISC_EXPENSE"
                    )

        # Map payment method to system account
        from app.services.accounting_engine import AccountingEngine
        payment_account = AccountingEngine._payment_method_account(payment_method)

        async with get_serializable_transaction() as conn:
            # Create expense record
            row = await conn.fetchrow(
                """INSERT INTO expenses
                    (restaurant_id, branch_id, category_id, category_name,
                     vendor_id, vendor_name,
                     amount, tax_amount, total_amount,
                     payment_method, payment_status, paid_amount,
                     expense_date, description, receipt_url, invoice_number,
                     is_recurring, recurrence, created_by)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
                RETURNING id""",
                UUID(restaurant_id),
                UUID(branch_id) if branch_id else None,
                UUID(category_id) if category_id else None,
                category_name,
                UUID(vendor_id) if vendor_id else None,
                vendor_name,
                float(amt), float(tax), float(total),
                payment_method,
                payment_status,
                float(total) if payment_status == "paid" else 0,
                expense_date or date.today(),
                description, receipt_url, invoice_number,
                is_recurring, recurrence, created_by,
            )
            expense_id = str(row["id"])

            # Post journal entry (if paid)
            journal_id = None
            if payment_status == "paid":
                from app.services.accounting_engine import accounting_engine

                lines = [
                    {"account": expense_account, "debit": float(amt),
                     "credit": 0, "description": description or f"Expense: {category_name}"},
                    {"account": payment_account, "debit": 0,
                     "credit": float(total),
                     "description": f"Paid — {description or category_name}"},
                ]

                # Tax deducted at source / input credit on expenses
                if tax > 0:
                    lines[1]["credit"] = float(amt)  # payment = net amount
                    lines.append({
                        "account": "CGST_PAYABLE", "debit": float(tax / 2),
                        "credit": 0, "description": "Input CGST on expense",
                    })
                    lines.append({
                        "account": "SGST_PAYABLE", "debit": float(tax / 2),
                        "credit": 0, "description": "Input SGST on expense",
                    })

                journal_id = await accounting_engine.create_journal_entry(
                    reference_type="expense",
                    reference_id=expense_id,
                    restaurant_id=restaurant_id,
                    branch_id=branch_id,
                    description=description or f"Expense: {category_name}",
                    created_by=created_by,
                    entry_date=expense_date or date.today(),
                    lines=lines,
                )

                if journal_id:
                    await conn.execute(
                        "UPDATE expenses SET journal_entry_id = $1 WHERE id = $2",
                        UUID(journal_id), UUID(expense_id),
                    )

            # Post supplier sub-ledger entry (if vendor)
            if vendor_id and journal_id:
                from app.services.subledger_service import subledger_service
                await subledger_service.post_supplier_entry(
                    restaurant_id=restaurant_id,
                    supplier_id=vendor_id,
                    journal_entry_id=journal_id,
                    debit=float(total) if payment_status == "paid" else 0,
                    credit=float(total),
                    reference_type="expense",
                    reference_id=expense_id,
                    description=description,
                    entry_date=expense_date or date.today(),
                )

        return {
            "id": expense_id,
            "total_amount": float(total),
            "payment_status": payment_status,
            "journal_entry_id": journal_id,
        }

    async def approve_expense(
        self,
        expense_id: str,
        restaurant_id: str,
        approved_by: str,
    ) -> dict:
        async with get_connection() as conn:
            await conn.execute(
                """UPDATE expenses SET approved_by = $1, approved_at = NOW(),
                       updated_at = NOW()
                   WHERE id = $2 AND restaurant_id = $3""",
                approved_by, UUID(expense_id), UUID(restaurant_id),
            )
        return {"expense_id": expense_id, "approved_by": approved_by}

    async def get_expense(self, expense_id: str, restaurant_id: str) -> dict:
        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM expenses WHERE id = $1 AND restaurant_id = $2",
                UUID(expense_id), UUID(restaurant_id),
            )
            if not row:
                raise ValidationError("Expense not found")
        return dict(row)

    async def list_expenses(
        self,
        restaurant_id: str,
        category_id: Optional[str] = None,
        vendor_id: Optional[str] = None,
        payment_status: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        conditions = ["restaurant_id = $1"]
        params: list = [UUID(restaurant_id)]
        idx = 2

        if category_id:
            conditions.append(f"category_id = ${idx}")
            params.append(UUID(category_id))
            idx += 1
        if vendor_id:
            conditions.append(f"vendor_id = ${idx}")
            params.append(UUID(vendor_id))
            idx += 1
        if payment_status:
            conditions.append(f"payment_status = ${idx}")
            params.append(payment_status)
            idx += 1
        if from_date:
            conditions.append(f"expense_date >= ${idx}")
            params.append(from_date)
            idx += 1
        if to_date:
            conditions.append(f"expense_date <= ${idx}")
            params.append(to_date)
            idx += 1

        where = " AND ".join(conditions)

        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""SELECT id, category_name, vendor_name, amount, tax_amount,
                       total_amount, payment_method, payment_status, expense_date,
                       description, is_recurring, approved_by, created_at
                FROM expenses WHERE {where}
                ORDER BY expense_date DESC, created_at DESC
                LIMIT ${idx} OFFSET ${idx+1}""",
                *params, limit, offset,
            )
        return [dict(r) for r in rows]

    async def expense_summary(
        self,
        restaurant_id: str,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
    ) -> dict:
        """Expense summary by category for a date range."""
        d_from = from_date or date.today().replace(day=1)
        d_to = to_date or date.today()

        async with get_connection() as conn:
            rows = await conn.fetch(
                """SELECT COALESCE(category_name, 'Uncategorized') AS category,
                       COUNT(*) AS count,
                       SUM(total_amount) AS total
                FROM expenses
                WHERE restaurant_id = $1
                  AND expense_date BETWEEN $2 AND $3
                GROUP BY category_name
                ORDER BY total DESC""",
                UUID(restaurant_id), d_from, d_to,
            )
            total = await conn.fetchval(
                """SELECT COALESCE(SUM(total_amount), 0)
                FROM expenses
                WHERE restaurant_id = $1
                  AND expense_date BETWEEN $2 AND $3""",
                UUID(restaurant_id), d_from, d_to,
            )
        return {
            "period": {"from": str(d_from), "to": str(d_to)},
            "total": float(total),
            "by_category": [dict(r) for r in rows],
        }


# Singleton
expense_service = ExpenseService()
