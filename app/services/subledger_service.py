"""
Sub-Ledger Service — AR/AP with running balances and aging.

Tracks every debit/credit against a customer (AR) or supplier (AP),
maintains running balance, and provides aging analysis (30/60/90 days).

Usage:
    from app.services.subledger_service import subledger_service
    await subledger_service.post_customer_entry(...)
    aging = await subledger_service.customer_aging(restaurant_id, customer_id)
"""
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from uuid import UUID

from app.core.database import get_connection
from app.core.logging import get_logger

logger = get_logger(__name__)


def _q(val) -> Decimal:
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class SubledgerService:
    """AR/AP sub-ledger with running balances and aging reports."""

    # ── Customer ledger (Accounts Receivable) ────────────────────────────────

    async def post_customer_entry(
        self,
        *,
        restaurant_id: str,
        customer_id: str,
        journal_entry_id: str,
        debit: float = 0,
        credit: float = 0,
        reference_type: str,
        reference_id: str = None,
        description: str = "",
        entry_date: Optional[date] = None,
    ) -> dict:
        """
        Post a debit or credit to a customer's sub-ledger.
        Automatically computes running balance.

        AR convention: debit = customer owes more, credit = customer paid
        """
        d = float(_q(debit))
        c = float(_q(credit))

        async with get_connection() as conn:
            # Get current balance
            row = await conn.fetchrow(
                """SELECT COALESCE(
                    (SELECT balance_after FROM customer_ledger
                     WHERE restaurant_id = $1 AND customer_id = $2
                     ORDER BY created_at DESC, id DESC LIMIT 1),
                    0
                ) AS balance""",
                UUID(restaurant_id), UUID(customer_id),
            )
            current = float(row["balance"])
            new_balance = round(current + d - c, 2)

            row = await conn.fetchrow(
                """INSERT INTO customer_ledger
                    (restaurant_id, customer_id, journal_entry_id,
                     debit, credit, balance_after,
                     reference_type, reference_id, description, entry_date)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                RETURNING id, balance_after""",
                UUID(restaurant_id), UUID(customer_id), UUID(journal_entry_id),
                d, c, new_balance,
                reference_type, reference_id, description,
                entry_date or date.today(),
            )

        return {"id": str(row["id"]), "balance_after": float(row["balance_after"])}

    async def get_customer_balance(
        self, restaurant_id: str, customer_id: str,
    ) -> float:
        """Get current AR balance for a customer. Positive = customer owes."""
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """SELECT COALESCE(
                    (SELECT balance_after FROM customer_ledger
                     WHERE restaurant_id = $1 AND customer_id = $2
                     ORDER BY created_at DESC, id DESC LIMIT 1),
                    0
                ) AS balance""",
                UUID(restaurant_id), UUID(customer_id),
            )
        return float(row["balance"])

    async def get_customer_ledger(
        self,
        restaurant_id: str,
        customer_id: str,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Get ledger entries for a customer, most recent first."""
        conditions = ["restaurant_id = $1", "customer_id = $2"]
        params: list = [UUID(restaurant_id), UUID(customer_id)]
        idx = 3

        if from_date:
            conditions.append(f"entry_date >= ${idx}")
            params.append(from_date)
            idx += 1
        if to_date:
            conditions.append(f"entry_date <= ${idx}")
            params.append(to_date)
            idx += 1

        where = " AND ".join(conditions)

        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""SELECT id, debit, credit, balance_after,
                       reference_type, reference_id, description,
                       entry_date, created_at
                FROM customer_ledger
                WHERE {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ${idx} OFFSET ${idx+1}""",
                *params, limit, offset,
            )
        return [dict(r) for r in rows]

    async def customer_aging(
        self,
        restaurant_id: str,
        customer_id: Optional[str] = None,
        as_of: Optional[date] = None,
    ) -> list[dict]:
        """
        AR aging report — groups outstanding balances into buckets:
          current (0-30), 31-60, 61-90, 90+

        If customer_id is None, returns aging for ALL customers.
        """
        ref_date = as_of or date.today()
        d30 = ref_date - timedelta(days=30)
        d60 = ref_date - timedelta(days=60)
        d90 = ref_date - timedelta(days=90)

        customer_filter = ""
        params: list = [UUID(restaurant_id), ref_date, d30, d60, d90]
        if customer_id:
            customer_filter = "AND cl.customer_id = $6"
            params.append(UUID(customer_id))

        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""
                WITH outstanding AS (
                    SELECT DISTINCT ON (customer_id)
                        customer_id, balance_after
                    FROM customer_ledger
                    WHERE restaurant_id = $1
                      AND entry_date <= $2
                      {customer_filter}
                    ORDER BY customer_id, created_at DESC, id DESC
                ),
                receivables AS (
                    SELECT cl.customer_id,
                        cl.debit - cl.credit AS net,
                        cl.entry_date
                    FROM customer_ledger cl
                    WHERE cl.restaurant_id = $1
                      AND cl.debit > cl.credit
                      {customer_filter}
                )
                SELECT
                    o.customer_id,
                    o.balance_after AS total_outstanding,
                    COALESCE(SUM(CASE WHEN r.entry_date > $3 THEN r.net END), 0) AS current_0_30,
                    COALESCE(SUM(CASE WHEN r.entry_date <= $3 AND r.entry_date > $4 THEN r.net END), 0) AS days_31_60,
                    COALESCE(SUM(CASE WHEN r.entry_date <= $4 AND r.entry_date > $5 THEN r.net END), 0) AS days_61_90,
                    COALESCE(SUM(CASE WHEN r.entry_date <= $5 THEN r.net END), 0) AS over_90
                FROM outstanding o
                LEFT JOIN receivables r ON r.customer_id = o.customer_id
                WHERE o.balance_after > 0
                GROUP BY o.customer_id, o.balance_after
                ORDER BY o.balance_after DESC
                """,
                *params,
            )
        return [dict(r) for r in rows]

    async def all_customer_balances(
        self, restaurant_id: str,
    ) -> list[dict]:
        """Get current balance for all customers with non-zero AR."""
        async with get_connection() as conn:
            rows = await conn.fetch(
                """SELECT DISTINCT ON (customer_id)
                    customer_id, balance_after
                FROM customer_ledger
                WHERE restaurant_id = $1
                ORDER BY customer_id, created_at DESC, id DESC""",
                UUID(restaurant_id),
            )
        return [
            {"customer_id": str(r["customer_id"]), "balance": float(r["balance_after"])}
            for r in rows if r["balance_after"] != 0
        ]

    # ── Supplier ledger (Accounts Payable) ───────────────────────────────────

    async def post_supplier_entry(
        self,
        *,
        restaurant_id: str,
        supplier_id: str,
        journal_entry_id: str,
        debit: float = 0,
        credit: float = 0,
        reference_type: str,
        reference_id: str = None,
        description: str = "",
        entry_date: Optional[date] = None,
    ) -> dict:
        """
        Post a debit or credit to a supplier's sub-ledger.
        AP convention: credit = we owe more, debit = we paid
        """
        d = float(_q(debit))
        c = float(_q(credit))

        async with get_connection() as conn:
            row = await conn.fetchrow(
                """SELECT COALESCE(
                    (SELECT balance_after FROM supplier_ledger
                     WHERE restaurant_id = $1 AND supplier_id = $2
                     ORDER BY created_at DESC, id DESC LIMIT 1),
                    0
                ) AS balance""",
                UUID(restaurant_id), UUID(supplier_id),
            )
            current = float(row["balance"])
            new_balance = round(current + c - d, 2)  # AP: credit-normal

            row = await conn.fetchrow(
                """INSERT INTO supplier_ledger
                    (restaurant_id, supplier_id, journal_entry_id,
                     debit, credit, balance_after,
                     reference_type, reference_id, description, entry_date)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                RETURNING id, balance_after""",
                UUID(restaurant_id), UUID(supplier_id), UUID(journal_entry_id),
                d, c, new_balance,
                reference_type, reference_id, description,
                entry_date or date.today(),
            )

        return {"id": str(row["id"]), "balance_after": float(row["balance_after"])}

    async def get_supplier_balance(
        self, restaurant_id: str, supplier_id: str,
    ) -> float:
        """Get current AP balance for a supplier. Positive = we owe them."""
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """SELECT COALESCE(
                    (SELECT balance_after FROM supplier_ledger
                     WHERE restaurant_id = $1 AND supplier_id = $2
                     ORDER BY created_at DESC, id DESC LIMIT 1),
                    0
                ) AS balance""",
                UUID(restaurant_id), UUID(supplier_id),
            )
        return float(row["balance"])

    async def get_supplier_ledger(
        self,
        restaurant_id: str,
        supplier_id: str,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Get ledger entries for a supplier, most recent first."""
        conditions = ["restaurant_id = $1", "supplier_id = $2"]
        params: list = [UUID(restaurant_id), UUID(supplier_id)]
        idx = 3

        if from_date:
            conditions.append(f"entry_date >= ${idx}")
            params.append(from_date)
            idx += 1
        if to_date:
            conditions.append(f"entry_date <= ${idx}")
            params.append(to_date)
            idx += 1

        where = " AND ".join(conditions)

        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""SELECT id, debit, credit, balance_after,
                       reference_type, reference_id, description,
                       entry_date, created_at
                FROM supplier_ledger
                WHERE {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ${idx} OFFSET ${idx+1}""",
                *params, limit, offset,
            )
        return [dict(r) for r in rows]

    async def supplier_aging(
        self,
        restaurant_id: str,
        supplier_id: Optional[str] = None,
        as_of: Optional[date] = None,
    ) -> list[dict]:
        """AP aging report — outstanding payables in 30/60/90+ buckets."""
        ref_date = as_of or date.today()
        d30 = ref_date - timedelta(days=30)
        d60 = ref_date - timedelta(days=60)
        d90 = ref_date - timedelta(days=90)

        supplier_filter = ""
        params: list = [UUID(restaurant_id), ref_date, d30, d60, d90]
        if supplier_id:
            supplier_filter = "AND sl.supplier_id = $6"
            params.append(UUID(supplier_id))

        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""
                WITH outstanding AS (
                    SELECT DISTINCT ON (supplier_id)
                        supplier_id, balance_after
                    FROM supplier_ledger
                    WHERE restaurant_id = $1
                      AND entry_date <= $2
                      {supplier_filter}
                    ORDER BY supplier_id, created_at DESC, id DESC
                ),
                payables AS (
                    SELECT sl.supplier_id,
                        sl.credit - sl.debit AS net,
                        sl.entry_date
                    FROM supplier_ledger sl
                    WHERE sl.restaurant_id = $1
                      AND sl.credit > sl.debit
                      {supplier_filter}
                )
                SELECT
                    o.supplier_id,
                    o.balance_after AS total_outstanding,
                    COALESCE(SUM(CASE WHEN p.entry_date > $3 THEN p.net END), 0) AS current_0_30,
                    COALESCE(SUM(CASE WHEN p.entry_date <= $3 AND p.entry_date > $4 THEN p.net END), 0) AS days_31_60,
                    COALESCE(SUM(CASE WHEN p.entry_date <= $4 AND p.entry_date > $5 THEN p.net END), 0) AS days_61_90,
                    COALESCE(SUM(CASE WHEN p.entry_date <= $5 THEN p.net END), 0) AS over_90
                FROM outstanding o
                LEFT JOIN payables p ON p.supplier_id = o.supplier_id
                WHERE o.balance_after > 0
                GROUP BY o.supplier_id, o.balance_after
                ORDER BY o.balance_after DESC
                """,
                *params,
            )
        return [dict(r) for r in rows]

    async def all_supplier_balances(
        self, restaurant_id: str,
    ) -> list[dict]:
        """Get current balance for all suppliers with non-zero AP."""
        async with get_connection() as conn:
            rows = await conn.fetch(
                """SELECT DISTINCT ON (supplier_id)
                    supplier_id, balance_after
                FROM supplier_ledger
                WHERE restaurant_id = $1
                ORDER BY supplier_id, created_at DESC, id DESC""",
                UUID(restaurant_id),
            )
        return [
            {"supplier_id": str(r["supplier_id"]), "balance": float(r["balance_after"])}
            for r in rows if r["balance_after"] != 0
        ]


# Singleton
subledger_service = SubledgerService()
