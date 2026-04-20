"""
Accounting Engine — Single source of truth for all financial entries.

Architecture
───────────────────────────────────────────────────────────────────────────────
Every financial event (order, payment, refund, inventory) MUST go through
this engine. No other code writes to journal_entries / journal_lines.

Guarantees:
  1. Double-entry: every journal entry balances (debit == credit)
  2. Idempotent: same (reference_type, reference_id) never duplicates
  3. Immutable: entries are never edited, only reversed
  4. Atomic: journal header + all lines in one transaction
  5. Auditable: all events logged to erp_event_log

Usage:
  from app.services.accounting_engine import accounting_engine
  journal_id = await accounting_engine.create_journal_entry(...)
───────────────────────────────────────────────────────────────────────────────
"""
import json as _json
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from uuid import UUID

from app.core.database import get_connection, get_serializable_transaction
from app.core.exceptions import ValidationError, ConflictError
from app.core.logging import get_logger

logger = get_logger(__name__)

# ── System account codes ─────────────────────────────────────────────────────
# Maps logical names → (system_code, account_code fallback)
SYSTEM_ACCOUNTS = {
    "CASH":                 ("CASH_ACCOUNT",        "1001"),
    "BANK":                 ("UPI_ACCOUNT",         "1002"),
    "CARD":                 ("CARD_ACCOUNT",        "1003"),
    "ACCOUNTS_RECEIVABLE":  ("ACCOUNTS_RECEIVABLE", "1003"),
    "INVENTORY_FOOD":       ("INVENTORY_FOOD",      "1004"),
    "INVENTORY_BEVERAGE":   ("INVENTORY_BEVERAGE",  "1005"),
    "ACCOUNTS_PAYABLE":     ("ACCOUNTS_PAYABLE",    "2001"),
    "CGST_PAYABLE":         ("CGST_PAYABLE",        "2002"),
    "SGST_PAYABLE":         ("SGST_PAYABLE",        "2003"),
    "IGST_PAYABLE":         ("IGST_PAYABLE",        "2004"),
    "FOOD_SALES":           ("FOOD_SALES",          "4001"),
    "BEVERAGE_SALES":       ("BEVERAGE_SALES",      "4002"),
    "COGS_FOOD":            ("COGS_FOOD",           "5001"),
    "COGS_BEVERAGE":        ("COGS_BEVERAGE",       "5002"),
    "DISCOUNT_EXPENSE":     ("DISCOUNT_EXPENSE",    "5006"),
    "SALES_RETURNS":        ("SALES_RETURNS",       "5007"),
}

VALID_REFERENCE_TYPES = {
    "order", "payment", "refund", "discount",
    "grn", "inventory_consumption", "inventory_adjustment",
    "vendor_payment", "expense", "reversal",
}


def _quantize(val) -> Decimal:
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ── Account resolver ─────────────────────────────────────────────────────────

async def _resolve_account(conn, restaurant_id: UUID, code: str) -> UUID:
    """
    Resolve a logical account name (e.g. 'CASH', 'FOOD_SALES') to its UUID.
    Tries system_code first, then account_code fallback.
    """
    mapping = SYSTEM_ACCOUNTS.get(code)
    if not mapping:
        raise ValidationError(f"Unknown account code: {code!r}")

    system_code, account_code = mapping

    row = await conn.fetchrow(
        "SELECT id FROM chart_of_accounts "
        "WHERE restaurant_id = $1 AND system_code = $2 AND is_active = true LIMIT 1",
        restaurant_id, system_code,
    )
    if row:
        return row["id"]

    row = await conn.fetchrow(
        "SELECT id FROM chart_of_accounts "
        "WHERE restaurant_id = $1 AND account_code = $2 AND is_active = true LIMIT 1",
        restaurant_id, account_code,
    )
    if row:
        return row["id"]

    raise ValidationError(
        f"System account '{code}' ({system_code}/{account_code}) not found "
        f"for restaurant {restaurant_id}. Run migration 018 or seed CoA."
    )


# ── Core engine ──────────────────────────────────────────────────────────────

class AccountingEngine:

    async def create_journal_entry(
        self,
        *,
        reference_type: str,
        reference_id: str,
        restaurant_id: str,
        branch_id: Optional[str] = None,
        lines: list[dict],
        description: str = "",
        created_by: str = "system",
        entry_date: Optional[date] = None,
        is_reversal: bool = False,
        reversed_entry_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Create a balanced, idempotent journal entry.

        Args:
            reference_type: order | payment | refund | discount | grn | expense | ...
            reference_id: unique ID of the source event (order_id, payment_id, etc.)
            restaurant_id: UUID string
            branch_id: optional UUID string
            lines: list of {account: str, debit: float, credit: float}
                   where 'account' is a key from SYSTEM_ACCOUNTS
            description: human-readable description
            created_by: user_id or 'system'
            entry_date: defaults to today
            is_reversal: True if this reverses another entry
            reversed_entry_id: UUID of the entry being reversed

        Returns:
            journal_entry_id (str) on success, None if skipped.

        Raises:
            ValidationError: unbalanced, missing accounts, bad input
            ConflictError: duplicate reference (idempotency guard)
        """
        if not reference_id or not restaurant_id:
            return None

        if reference_type not in VALID_REFERENCE_TYPES:
            raise ValidationError(
                f"Invalid reference_type: {reference_type!r}. "
                f"Must be one of: {', '.join(sorted(VALID_REFERENCE_TYPES))}"
            )

        if not lines:
            raise ValidationError("Journal entry must have at least one line")

        # Validate and quantize
        total_debit = Decimal("0")
        total_credit = Decimal("0")
        for line in lines:
            d = _quantize(line.get("debit", 0))
            c = _quantize(line.get("credit", 0))
            if d < 0 or c < 0:
                raise ValidationError("Debit and credit must be >= 0")
            if d > 0 and c > 0:
                raise ValidationError("A line cannot have both debit and credit > 0")
            if d == 0 and c == 0:
                raise ValidationError("A line must have either debit or credit > 0")
            total_debit += d
            total_credit += c

        if total_debit != total_credit:
            raise ValidationError(
                f"Unbalanced entry: debit={total_debit}, credit={total_credit}"
            )

        restaurant_uuid = UUID(restaurant_id)
        branch_uuid = UUID(branch_id) if branch_id else None
        reversed_uuid = UUID(reversed_entry_id) if reversed_entry_id else None

        async with get_serializable_transaction() as conn:
            # Idempotency check
            existing = await conn.fetchval(
                "SELECT id FROM journal_entries "
                "WHERE restaurant_id = $1 AND reference_type = $2 AND reference_id = $3",
                restaurant_uuid, reference_type, reference_id,
            )
            if existing:
                logger.info(
                    "journal_entry_idempotent_skip",
                    reference_type=reference_type,
                    reference_id=reference_id,
                    existing_id=str(existing),
                )
                return str(existing)

            # Resolve all account codes → UUIDs
            resolved_lines = []
            for line in lines:
                account_id = await _resolve_account(conn, restaurant_uuid, line["account"])
                resolved_lines.append({
                    "account_id": str(account_id),
                    "debit": float(_quantize(line.get("debit", 0))),
                    "credit": float(_quantize(line.get("credit", 0))),
                    "description": line.get("description", ""),
                })

            # Use the existing fn_create_journal_entry for atomic insert
            journal_id = await conn.fetchval(
                """
                SELECT fn_create_journal_entry(
                    $1, $2, $3, $4, $5, $6, $7, $8::jsonb
                )
                """,
                restaurant_uuid,
                branch_uuid,
                entry_date or date.today(),
                reference_type,
                reference_id,
                description,
                created_by,
                _json.dumps(resolved_lines),
            )

            # Set reversal fields if applicable
            if is_reversal and reversed_uuid:
                await conn.execute(
                    "UPDATE journal_entries SET is_reversed = false, reversed_entry_id = $1 WHERE id = $2",
                    reversed_uuid, journal_id,
                )
                await conn.execute(
                    "UPDATE journal_entries SET is_reversed = true, reversed_by = $1 WHERE id = $2",
                    journal_id, reversed_uuid,
                )

            # Audit log
            try:
                await conn.execute(
                    """INSERT INTO erp_event_log
                           (restaurant_id, event_type, reference_type, reference_id, status)
                       VALUES ($1, 'accounting.journal_entry_created', $2, $3, 'completed')""",
                    restaurant_uuid, reference_type, reference_id,
                )
            except Exception:
                pass  # audit log failure must not break accounting

        logger.info(
            "journal_entry_created",
            journal_id=str(journal_id),
            reference_type=reference_type,
            reference_id=reference_id,
            total=float(total_debit),
        )
        return str(journal_id)

    # ── Reversal ─────────────────────────────────────────────────────────────

    async def reverse_entry(
        self,
        *,
        journal_entry_id: str,
        reason: str = "",
        created_by: str = "system",
    ) -> str:
        """
        Reverse an existing journal entry by creating a new entry with
        swapped debit/credit. The original is marked is_reversed=True.

        Returns the new (reversal) journal_entry_id.
        Raises ValidationError if already reversed.
        """
        entry_uuid = UUID(journal_entry_id)

        async with get_serializable_transaction() as conn:
            entry = await conn.fetchrow(
                "SELECT * FROM journal_entries WHERE id = $1 FOR UPDATE",
                entry_uuid,
            )
            if not entry:
                raise ValidationError(f"Journal entry {journal_entry_id} not found")
            if entry["is_reversed"]:
                raise ValidationError(f"Journal entry {journal_entry_id} is already reversed")

            # Get original lines
            original_lines = await conn.fetch(
                "SELECT account_id, debit, credit, description FROM journal_lines WHERE journal_entry_id = $1",
                entry_uuid,
            )
            if not original_lines:
                raise ValidationError("Journal entry has no lines")

            # Build reversal lines (swap debit ↔ credit)
            reversal_lines = []
            for ol in original_lines:
                reversal_lines.append({
                    "account_id": str(ol["account_id"]),
                    "debit": float(ol["credit"]),      # swap
                    "credit": float(ol["debit"]),       # swap
                    "description": f"REVERSAL: {ol['description'] or ''}",
                })

            ref_type = "reversal"
            ref_id = f"rev_{journal_entry_id}"
            description = f"Reversal of {entry['reference_type']} {entry['reference_id']}"
            if reason:
                description += f" — {reason}"

            reversal_id = await conn.fetchval(
                "SELECT fn_create_journal_entry($1, $2, $3, $4, $5, $6, $7, $8::jsonb)",
                entry["restaurant_id"],
                entry["branch_id"],
                date.today(),
                ref_type,
                ref_id,
                description,
                created_by,
                _json.dumps(reversal_lines),
            )

            # Link the two entries
            await conn.execute(
                "UPDATE journal_entries SET is_reversed = true, reversed_by = $1 WHERE id = $2",
                reversal_id, entry_uuid,
            )
            await conn.execute(
                "UPDATE journal_entries SET reversed_entry_id = $1 WHERE id = $2",
                entry_uuid, reversal_id,
            )

        logger.info("journal_entry_reversed", original=journal_entry_id, reversal=str(reversal_id))
        return str(reversal_id)

    # ══════════════════════════════════════════════════════════════════════════
    # EVENT-SPECIFIC JOURNAL BUILDERS
    # These are called by event handlers. Each one maps a business event
    # to its correct double-entry pattern.
    # ══════════════════════════════════════════════════════════════════════════

    async def record_order_created(
        self,
        *,
        restaurant_id: str,
        branch_id: Optional[str],
        order_id: str,
        total_amount: float,
        created_by: str = "system",
    ) -> Optional[str]:
        """
        Order created → DR Accounts Receivable, CR Sales Revenue.
        This records the obligation, NOT the cash receipt.
        """
        amt = _quantize(total_amount)
        if amt <= 0:
            return None

        return await self.create_journal_entry(
            reference_type="order",
            reference_id=order_id,
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            description=f"Sale — order {order_id}",
            created_by=created_by,
            lines=[
                {"account": "ACCOUNTS_RECEIVABLE", "debit": float(amt), "credit": 0,
                 "description": f"Order {order_id} receivable"},
                {"account": "FOOD_SALES", "debit": 0, "credit": float(amt),
                 "description": f"Order {order_id} revenue"},
            ],
        )

    async def record_payment(
        self,
        *,
        restaurant_id: str,
        branch_id: Optional[str],
        payment_id: str,
        order_id: str,
        amount: float,
        method: str = "cash",
        created_by: str = "system",
    ) -> Optional[str]:
        """
        Payment received → DR Cash/Bank/Card, CR Accounts Receivable.
        Settles the receivable created when the order was booked.
        """
        amt = _quantize(amount)
        if amt <= 0:
            return None

        payment_account = self._payment_method_account(method)

        return await self.create_journal_entry(
            reference_type="payment",
            reference_id=payment_id,
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            description=f"Payment {payment_id} for order {order_id} ({method})",
            created_by=created_by,
            lines=[
                {"account": payment_account, "debit": float(amt), "credit": 0,
                 "description": f"Payment received ({method})"},
                {"account": "ACCOUNTS_RECEIVABLE", "debit": 0, "credit": float(amt),
                 "description": f"Receivable settled — order {order_id}"},
            ],
        )

    async def record_discount(
        self,
        *,
        restaurant_id: str,
        branch_id: Optional[str],
        order_id: str,
        discount_amount: float,
        created_by: str = "system",
    ) -> Optional[str]:
        """
        Discount applied → DR Discount Expense, CR Accounts Receivable.
        Reduces the outstanding receivable.
        """
        amt = _quantize(discount_amount)
        if amt <= 0:
            return None

        return await self.create_journal_entry(
            reference_type="discount",
            reference_id=f"disc_{order_id}",
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            description=f"Discount on order {order_id}",
            created_by=created_by,
            lines=[
                {"account": "DISCOUNT_EXPENSE", "debit": float(amt), "credit": 0,
                 "description": f"Discount — order {order_id}"},
                {"account": "ACCOUNTS_RECEIVABLE", "debit": 0, "credit": float(amt),
                 "description": f"Receivable reduced — discount on {order_id}"},
            ],
        )

    async def record_refund(
        self,
        *,
        restaurant_id: str,
        branch_id: Optional[str],
        payment_id: str,
        order_id: str,
        amount: float,
        method: str = "cash",
        created_by: str = "system",
    ) -> Optional[str]:
        """
        Refund → DR Sales Returns, CR Cash/Bank.
        Records money going back to customer.
        """
        amt = _quantize(amount)
        if amt <= 0:
            return None

        payment_account = self._payment_method_account(method)

        return await self.create_journal_entry(
            reference_type="refund",
            reference_id=f"ref_{payment_id}",
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            description=f"Refund for order {order_id}",
            created_by=created_by,
            lines=[
                {"account": "SALES_RETURNS", "debit": float(amt), "credit": 0,
                 "description": f"Refund — order {order_id}"},
                {"account": payment_account, "debit": 0, "credit": float(amt),
                 "description": f"Cash refunded ({method})"},
            ],
        )

    async def record_cogs(
        self,
        *,
        restaurant_id: str,
        branch_id: Optional[str],
        order_id: str,
        amount: float,
        created_by: str = "system",
    ) -> Optional[str]:
        """
        Order fulfilled (COGS) → DR Cost of Goods Sold, CR Inventory.
        Records the cost of ingredients consumed.
        """
        amt = _quantize(amount)
        if amt <= 0:
            return None

        return await self.create_journal_entry(
            reference_type="inventory_consumption",
            reference_id=f"cogs_{order_id}",
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            description=f"COGS for order {order_id}",
            created_by=created_by,
            lines=[
                {"account": "COGS_FOOD", "debit": float(amt), "credit": 0,
                 "description": f"Cost of goods — order {order_id}"},
                {"account": "INVENTORY_FOOD", "debit": 0, "credit": float(amt),
                 "description": f"Inventory consumed — order {order_id}"},
            ],
        )

    async def record_grn(
        self,
        *,
        restaurant_id: str,
        branch_id: Optional[str],
        grn_id: str,
        amount: float,
        created_by: str = "system",
    ) -> Optional[str]:
        """
        GRN verified → DR Inventory, CR Accounts Payable.
        Records inventory received from vendor (on credit).
        """
        amt = _quantize(amount)
        if amt <= 0:
            return None

        return await self.create_journal_entry(
            reference_type="grn",
            reference_id=grn_id,
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            description=f"GRN {grn_id} — inventory received",
            created_by=created_by,
            lines=[
                {"account": "INVENTORY_FOOD", "debit": float(amt), "credit": 0,
                 "description": f"Inventory in — GRN {grn_id}"},
                {"account": "ACCOUNTS_PAYABLE", "debit": 0, "credit": float(amt),
                 "description": f"Payable to vendor — GRN {grn_id}"},
            ],
        )

    async def record_vendor_payment(
        self,
        *,
        restaurant_id: str,
        branch_id: Optional[str],
        payment_id: str,
        vendor_id: str,
        amount: float,
        method: str = "cash",
        created_by: str = "system",
    ) -> Optional[str]:
        """
        Vendor payment → DR Accounts Payable, CR Cash/Bank.
        Settles vendor payable.
        """
        amt = _quantize(amount)
        if amt <= 0:
            return None

        payment_account = self._payment_method_account(method)

        return await self.create_journal_entry(
            reference_type="vendor_payment",
            reference_id=payment_id,
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            description=f"Vendor payment {payment_id} to {vendor_id}",
            created_by=created_by,
            lines=[
                {"account": "ACCOUNTS_PAYABLE", "debit": float(amt), "credit": 0,
                 "description": f"Payable settled — vendor {vendor_id}"},
                {"account": payment_account, "debit": 0, "credit": float(amt),
                 "description": f"Paid to vendor ({method})"},
            ],
        )

    async def record_expense(
        self,
        *,
        restaurant_id: str,
        branch_id: Optional[str],
        expense_id: str,
        amount: float,
        expense_account: str = "COGS_FOOD",
        payment_account: str = "CASH",
        description: str = "",
        created_by: str = "system",
    ) -> Optional[str]:
        """
        Manual expense → DR Expense Account, CR Cash/Bank.
        Used for rent, salaries, supplies, etc.
        """
        amt = _quantize(amount)
        if amt <= 0:
            return None

        return await self.create_journal_entry(
            reference_type="expense",
            reference_id=expense_id,
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            description=description or f"Expense {expense_id}",
            created_by=created_by,
            lines=[
                {"account": expense_account, "debit": float(amt), "credit": 0,
                 "description": description},
                {"account": payment_account, "debit": 0, "credit": float(amt),
                 "description": f"Paid — {description}"},
            ],
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _payment_method_account(method: str) -> str:
        """Map payment method string to system account code."""
        method = (method or "cash").lower()
        if method in ("upi", "bank", "bank_transfer", "neft", "rtgs"):
            return "BANK"
        if method in ("card", "credit_card", "debit_card"):
            return "CARD"
        return "CASH"

    # ══════════════════════════════════════════════════════════════════════════
    # REPORTING QUERIES
    # ══════════════════════════════════════════════════════════════════════════

    async def get_trial_balance(
        self,
        restaurant_id: str,
        as_of_date: Optional[date] = None,
    ) -> dict:
        """
        Trial Balance — SUM(debit) and SUM(credit) per account.
        Balanced books: total_debit == total_credit (always).
        """
        from uuid import UUID
        restaurant_uuid = UUID(restaurant_id)
        if not as_of_date:
            as_of_date = date.today()

        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    coa.id,
                    coa.account_code,
                    coa.name,
                    coa.account_type,
                    COALESCE(SUM(jl.debit),  0) AS total_debit,
                    COALESCE(SUM(jl.credit), 0) AS total_credit
                FROM chart_of_accounts coa
                LEFT JOIN journal_lines jl ON jl.account_id = coa.id
                LEFT JOIN journal_entries je ON je.id = jl.journal_entry_id
                    AND je.entry_date <= $2
                    AND je.is_reversed = false
                WHERE coa.restaurant_id = $1
                  AND coa.is_active = true
                GROUP BY coa.id, coa.account_code, coa.name, coa.account_type
                HAVING COALESCE(SUM(jl.debit), 0) != 0 OR COALESCE(SUM(jl.credit), 0) != 0
                ORDER BY coa.account_code
                """,
                restaurant_uuid, as_of_date,
            )

        total_debit = Decimal("0")
        total_credit = Decimal("0")
        accounts = []
        for r in rows:
            d = Decimal(str(r["total_debit"]))
            c = Decimal(str(r["total_credit"]))
            total_debit += d
            total_credit += c
            accounts.append({
                "account_id": str(r["id"]),
                "account_code": r["account_code"],
                "name": r["name"],
                "account_type": r["account_type"],
                "debit": float(d),
                "credit": float(c),
                "balance": float(d - c),
            })

        return {
            "as_of_date": as_of_date.isoformat(),
            "total_debit": float(total_debit),
            "total_credit": float(total_credit),
            "is_balanced": total_debit == total_credit,
            "accounts": accounts,
        }

    async def get_balance_sheet(
        self,
        restaurant_id: str,
        as_of_date: Optional[date] = None,
    ) -> dict:
        """
        Balance Sheet — Assets = Liabilities + Equity.

        Computes net balance for every asset, liability, and equity account
        as of the given date.
        """
        from uuid import UUID
        restaurant_uuid = UUID(restaurant_id)
        if not as_of_date:
            as_of_date = date.today()

        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    coa.id,
                    coa.account_code,
                    coa.name,
                    coa.account_type,
                    COALESCE(SUM(jl.debit),  0) AS total_debit,
                    COALESCE(SUM(jl.credit), 0) AS total_credit
                FROM chart_of_accounts coa
                LEFT JOIN journal_lines jl ON jl.account_id = coa.id
                LEFT JOIN journal_entries je ON je.id = jl.journal_entry_id
                    AND je.entry_date <= $2
                    AND je.is_reversed = false
                WHERE coa.restaurant_id = $1
                  AND coa.is_active = true
                  AND coa.account_type IN ('asset', 'liability', 'equity')
                GROUP BY coa.id, coa.account_code, coa.name, coa.account_type
                HAVING COALESCE(SUM(jl.debit), 0) != 0 OR COALESCE(SUM(jl.credit), 0) != 0
                ORDER BY coa.account_code
                """,
                restaurant_uuid, as_of_date,
            )

            # Also compute retained earnings (revenue - expenses) for balance sheet
            retained = await conn.fetchrow(
                """
                SELECT
                    COALESCE(SUM(jl.credit) FILTER (WHERE coa.account_type = 'revenue'), 0)
                  - COALESCE(SUM(jl.debit)  FILTER (WHERE coa.account_type = 'revenue'), 0)
                  - COALESCE(SUM(jl.debit)  FILTER (WHERE coa.account_type = 'expense'), 0)
                  + COALESCE(SUM(jl.credit) FILTER (WHERE coa.account_type = 'expense'), 0)
                    AS retained_earnings
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                    AND je.entry_date <= $2 AND je.is_reversed = false
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND coa.account_type IN ('revenue', 'expense')
                """,
                restaurant_uuid, as_of_date,
            )

        assets = []
        liabilities = []
        equity = []
        total_assets = Decimal("0")
        total_liabilities = Decimal("0")
        total_equity = Decimal("0")

        for r in rows:
            d = Decimal(str(r["total_debit"]))
            c = Decimal(str(r["total_credit"]))
            entry = {
                "account_id": str(r["id"]),
                "account_code": r["account_code"],
                "name": r["name"],
            }

            if r["account_type"] == "asset":
                balance = d - c  # debit-normal
                entry["balance"] = float(balance)
                total_assets += balance
                assets.append(entry)
            elif r["account_type"] == "liability":
                balance = c - d  # credit-normal
                entry["balance"] = float(balance)
                total_liabilities += balance
                liabilities.append(entry)
            elif r["account_type"] == "equity":
                balance = c - d  # credit-normal
                entry["balance"] = float(balance)
                total_equity += balance
                equity.append(entry)

        retained_earnings = Decimal(str(retained["retained_earnings"])) if retained else Decimal("0")
        total_equity += retained_earnings

        return {
            "as_of_date": as_of_date.isoformat(),
            "assets": {"accounts": assets, "total": float(total_assets)},
            "liabilities": {"accounts": liabilities, "total": float(total_liabilities)},
            "equity": {
                "accounts": equity,
                "retained_earnings": float(retained_earnings),
                "total": float(total_equity),
            },
            "is_balanced": total_assets == (total_liabilities + total_equity),
            "total_liabilities_and_equity": float(total_liabilities + total_equity),
        }


# Singleton
accounting_engine = AccountingEngine()
