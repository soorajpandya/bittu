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
    "PG_CLEARING":          ("PG_CLEARING",         "1006"),
    "GATEWAY_CHARGES":      ("GATEWAY_CHARGES",     "5011"),
    "GATEWAY_TAX":          ("GATEWAY_TAX",         "5012"),
    "RENT_EXPENSE":         ("RENT_EXPENSE",        "5020"),
    "SALARY_EXPENSE":       ("SALARY_EXPENSE",      "5021"),
    "UTILITIES_EXPENSE":    ("UTILITIES_EXPENSE",   "5022"),
    "MISC_EXPENSE":         ("MISC_EXPENSE",        "5030"),
}

VALID_REFERENCE_TYPES = {
    "order", "payment", "refund", "discount",
    "grn", "inventory_consumption", "inventory_adjustment",
    "vendor_payment", "expense", "reversal",
    "shift_close", "period_close",
    "settlement", "gateway_fee",
    "invoice", "invoice_void", "tax_payment",
    "bank_recon", "partial_refund", "chargeback",
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
        source_event: Optional[str] = None,
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
        effective_date = entry_date or date.today()

        async with get_serializable_transaction() as conn:
            # ── Period lock enforcement ──────────────────────────────────
            locked_period = await conn.fetchrow(
                "SELECT id, status, period_start, period_end FROM accounting_periods "
                "WHERE restaurant_id = $1 AND status IN ('closed', 'locked') "
                "AND $2 BETWEEN period_start AND period_end LIMIT 1",
                restaurant_uuid, effective_date,
            )
            if locked_period:
                raise ValidationError(
                    f"Cannot create journal entry: accounting period "
                    f"{locked_period['period_start']} to {locked_period['period_end']} "
                    f"is {locked_period['status']}. Reopen the period first."
                )

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
                effective_date,
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

            # Set source_event for audit trail
            if source_event:
                await conn.execute(
                    "UPDATE journal_entries SET source_event = $1 WHERE id = $2",
                    source_event, journal_id,
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

            # ── Period lock: block reversal if original entry date is in closed period
            locked_period = await conn.fetchrow(
                "SELECT id, status, period_start, period_end FROM accounting_periods "
                "WHERE restaurant_id = $1 AND status IN ('closed', 'locked') "
                "AND $2 BETWEEN period_start AND period_end LIMIT 1",
                entry["restaurant_id"], entry["entry_date"],
            )
            if locked_period:
                raise ValidationError(
                    f"Cannot reverse journal entry: accounting period "
                    f"{locked_period['period_start']} to {locked_period['period_end']} "
                    f"is {locked_period['status']}. Reopen the period first."
                )

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
        Payment received → settles the receivable.

        For cash: DR Cash, CR Accounts Receivable (immediate)
        For online (upi/card/etc): DR PG Clearing, CR Accounts Receivable
          (money sits in gateway clearing until settlement arrives)
        """
        from app.services.settlement_service import is_online_payment

        amt = _quantize(amount)
        if amt <= 0:
            return None

        if is_online_payment(method):
            # Online payment → money goes to PG Clearing (not bank yet)
            debit_account = "PG_CLEARING"
            debit_desc = f"Gateway capture ({method})"
        else:
            # Cash → direct to cash account
            debit_account = self._payment_method_account(method)
            debit_desc = f"Payment received ({method})"

        return await self.create_journal_entry(
            reference_type="payment",
            reference_id=payment_id,
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            description=f"Payment {payment_id} for order {order_id} ({method})",
            created_by=created_by,
            source_event="PAYMENT_COMPLETED",
            lines=[
                {"account": debit_account, "debit": float(amt), "credit": 0,
                 "description": debit_desc},
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
        Refund → DR Sales Returns, CR Cash/PG Clearing.

        For cash: money leaves cash drawer
        For online: gateway processes refund from clearing
        """
        from app.services.settlement_service import is_online_payment

        amt = _quantize(amount)
        if amt <= 0:
            return None

        if is_online_payment(method):
            credit_account = "PG_CLEARING"
            credit_desc = f"Refund via gateway ({method})"
        else:
            credit_account = self._payment_method_account(method)
            credit_desc = f"Cash refunded ({method})"

        return await self.create_journal_entry(
            reference_type="refund",
            reference_id=f"ref_{payment_id}",
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            description=f"Refund for order {order_id}",
            created_by=created_by,
            source_event="PAYMENT_REFUNDED",
            lines=[
                {"account": "SALES_RETURNS", "debit": float(amt), "credit": 0,
                 "description": f"Refund — order {order_id}"},
                {"account": credit_account, "debit": 0, "credit": float(amt),
                 "description": credit_desc},
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

    # ══════════════════════════════════════════════════════════════════════════
    # EDGE CASE HANDLERS — Partial Refund, Split Payment, Chargeback
    # ══════════════════════════════════════════════════════════════════════════

    async def record_partial_refund(
        self,
        *,
        restaurant_id: str,
        branch_id: Optional[str],
        payment_id: str,
        order_id: str,
        refund_amount: float,
        original_amount: float,
        method: str = "cash",
        reason: str = "",
        created_by: str = "system",
    ) -> Optional[str]:
        """
        Partial refund — refund a portion of a payment.

        Creates a journal entry for only the refunded amount.
        DR Sales Returns (partial), CR Cash/PG Clearing (partial).
        Uses a unique reference_id so it won't conflict with full refund.
        """
        from app.services.settlement_service import is_online_payment

        amt = _quantize(refund_amount)
        orig = _quantize(original_amount)
        if amt <= 0 or amt > orig:
            raise ValidationError(
                f"Partial refund amount ({amt}) must be > 0 and <= original ({orig})"
            )

        if is_online_payment(method):
            credit_account = "PG_CLEARING"
        else:
            credit_account = self._payment_method_account(method)

        desc = f"Partial refund ₹{amt} of ₹{orig} for order {order_id}"
        if reason:
            desc += f" — {reason}"

        return await self.create_journal_entry(
            reference_type="partial_refund",
            reference_id=f"pref_{payment_id}_{amt}",
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            description=desc,
            created_by=created_by,
            source_event="PAYMENT_REFUNDED",
            lines=[
                {"account": "SALES_RETURNS", "debit": float(amt), "credit": 0,
                 "description": f"Partial refund — order {order_id}"},
                {"account": credit_account, "debit": 0, "credit": float(amt),
                 "description": f"Refunded ₹{amt} ({method})"},
            ],
        )

    async def record_split_payment(
        self,
        *,
        restaurant_id: str,
        branch_id: Optional[str],
        order_id: str,
        splits: list[dict],
        created_by: str = "system",
    ) -> list[str]:
        """
        Split payment — order paid via multiple methods.

        Each split: {method: "cash"|"upi"|"card", amount: float, payment_id: str}
        Creates one journal entry per split.

        Returns list of journal entry IDs.
        """
        from app.services.settlement_service import is_online_payment

        if not splits:
            raise ValidationError("Split payment requires at least one split")

        journal_ids = []
        for i, split in enumerate(splits):
            method = split.get("method", "cash")
            amount = _quantize(split.get("amount", 0))
            pid = split.get("payment_id", f"split_{order_id}_{i}")

            if amount <= 0:
                continue

            if is_online_payment(method):
                debit_account = "PG_CLEARING"
            else:
                debit_account = self._payment_method_account(method)

            jid = await self.create_journal_entry(
                reference_type="payment",
                reference_id=f"pay_{pid}",
                restaurant_id=restaurant_id,
                branch_id=branch_id,
                description=f"Split payment {i+1}/{len(splits)} for order {order_id} ({method} ₹{amount})",
                created_by=created_by,
                source_event="PAYMENT_COMPLETED",
                lines=[
                    {"account": debit_account, "debit": float(amount), "credit": 0,
                     "description": f"Payment received ({method})"},
                    {"account": "ACCOUNTS_RECEIVABLE", "debit": 0, "credit": float(amount),
                     "description": f"Split payment — order {order_id}"},
                ],
            )
            if jid:
                journal_ids.append(jid)

        return journal_ids

    async def record_chargeback(
        self,
        *,
        restaurant_id: str,
        branch_id: Optional[str],
        payment_id: str,
        order_id: str,
        amount: float,
        method: str = "card",
        reason: str = "",
        created_by: str = "system",
    ) -> Optional[str]:
        """
        Chargeback — customer disputes a card/online payment.

        DR Chargeback Loss (or Sales Returns), CR PG Clearing.
        The gateway claws back the money from our clearing account.
        """
        amt = _quantize(amount)
        if amt <= 0:
            return None

        desc = f"Chargeback for order {order_id} — payment {payment_id}"
        if reason:
            desc += f" — {reason}"

        return await self.create_journal_entry(
            reference_type="chargeback",
            reference_id=f"cb_{payment_id}",
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            description=desc,
            created_by=created_by,
            source_event="PAYMENT_REFUNDED",
            lines=[
                {"account": "SALES_RETURNS", "debit": float(amt), "credit": 0,
                 "description": f"Chargeback loss — order {order_id}"},
                {"account": "PG_CLEARING", "debit": 0, "credit": float(amt),
                 "description": f"Gateway chargeback clawback ({method})"},
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

    # ══════════════════════════════════════════════════════════════════════════
    # INCOME STATEMENT (P&L) — Pure ledger-based
    # ══════════════════════════════════════════════════════════════════════════

    async def get_income_statement(
        self,
        restaurant_id: str,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
    ) -> dict:
        """
        Income Statement (Profit & Loss) — derived ONLY from journal_lines.

        Revenue  = net credit on revenue accounts (credit - debit)
        Expenses = net debit on expense accounts  (debit - credit)
        Net Income = Revenue - Expenses
        """
        from uuid import UUID
        restaurant_uuid = UUID(restaurant_id)
        if not from_date:
            from_date = date.today().replace(day=1)
        if not to_date:
            to_date = date.today()

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
                JOIN journal_lines jl ON jl.account_id = coa.id
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                WHERE je.restaurant_id = $1
                  AND je.entry_date BETWEEN $2 AND $3
                  AND je.is_reversed = false
                  AND coa.account_type IN ('revenue', 'expense')
                  AND coa.is_active = true
                GROUP BY coa.id, coa.account_code, coa.name, coa.account_type
                ORDER BY coa.account_code
                """,
                restaurant_uuid, from_date, to_date,
            )

        revenue_accounts = []
        expense_accounts = []
        total_revenue = Decimal("0")
        total_expenses = Decimal("0")

        for r in rows:
            d = Decimal(str(r["total_debit"]))
            c = Decimal(str(r["total_credit"]))
            entry = {
                "account_id": str(r["id"]),
                "account_code": r["account_code"],
                "name": r["name"],
            }

            if r["account_type"] == "revenue":
                net = c - d  # revenue is credit-normal
                entry["amount"] = float(net)
                total_revenue += net
                revenue_accounts.append(entry)
            elif r["account_type"] == "expense":
                net = d - c  # expense is debit-normal
                entry["amount"] = float(net)
                total_expenses += net
                expense_accounts.append(entry)

        net_income = total_revenue - total_expenses

        return {
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "revenue": {"accounts": revenue_accounts, "total": float(total_revenue)},
            "expenses": {"accounts": expense_accounts, "total": float(total_expenses)},
            "net_income": float(net_income),
            "is_profit": net_income > 0,
        }

    # ══════════════════════════════════════════════════════════════════════════
    # PERIOD MANAGEMENT — Close / Reopen accounting periods
    # ══════════════════════════════════════════════════════════════════════════

    async def close_period(
        self,
        *,
        restaurant_id: str,
        period_start: date,
        period_end: date,
        closed_by: str,
        notes: str = "",
    ) -> dict:
        """
        Close an accounting period. No new journal entries can be created
        in the date range once closed. DB trigger enforces this.
        """
        from uuid import UUID
        restaurant_uuid = UUID(restaurant_id)

        async with get_serializable_transaction() as conn:
            # Check if period already exists
            existing = await conn.fetchrow(
                "SELECT id, status FROM accounting_periods "
                "WHERE restaurant_id = $1 AND period_start = $2 AND period_end = $3",
                restaurant_uuid, period_start, period_end,
            )

            if existing:
                if existing["status"] in ("closed", "locked"):
                    return {
                        "period_id": str(existing["id"]),
                        "status": existing["status"],
                        "message": "Period already closed",
                    }
                # Update existing open period to closed
                await conn.execute(
                    "UPDATE accounting_periods SET status = 'closed', closed_by = $1, "
                    "closed_at = NOW(), notes = $2, updated_at = NOW() WHERE id = $3",
                    closed_by, notes, existing["id"],
                )
                period_id = existing["id"]
            else:
                period_id = await conn.fetchval(
                    """INSERT INTO accounting_periods
                           (restaurant_id, period_start, period_end, status,
                            closed_by, closed_at, notes)
                       VALUES ($1, $2, $3, 'closed', $4, NOW(), $5)
                       RETURNING id""",
                    restaurant_uuid, period_start, period_end, closed_by, notes,
                )

        logger.info(
            "accounting_period_closed",
            restaurant_id=restaurant_id,
            period=f"{period_start} to {period_end}",
            closed_by=closed_by,
        )

        return {
            "period_id": str(period_id),
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "status": "closed",
        }

    async def reopen_period(
        self,
        *,
        restaurant_id: str,
        period_start: date,
        period_end: date,
        reopened_by: str,
        notes: str = "",
    ) -> dict:
        """
        Reopen a previously closed period. Locked periods cannot be reopened.
        """
        from uuid import UUID
        restaurant_uuid = UUID(restaurant_id)

        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                "SELECT id, status FROM accounting_periods "
                "WHERE restaurant_id = $1 AND period_start = $2 AND period_end = $3",
                restaurant_uuid, period_start, period_end,
            )

            if not existing:
                raise ValidationError("Period not found")

            if existing["status"] == "locked":
                raise ValidationError(
                    "Cannot reopen a locked period. Locked periods are permanent."
                )

            if existing["status"] == "open":
                return {
                    "period_id": str(existing["id"]),
                    "status": "open",
                    "message": "Period is already open",
                }

            await conn.execute(
                "UPDATE accounting_periods SET status = 'open', reopened_by = $1, "
                "reopened_at = NOW(), notes = $2, updated_at = NOW() WHERE id = $3",
                reopened_by, notes, existing["id"],
            )

        logger.info(
            "accounting_period_reopened",
            restaurant_id=restaurant_id,
            period=f"{period_start} to {period_end}",
            reopened_by=reopened_by,
        )

        return {
            "period_id": str(existing["id"]),
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "status": "open",
        }

    async def list_periods(
        self,
        restaurant_id: str,
    ) -> list[dict]:
        """List all accounting periods for a restaurant."""
        from uuid import UUID
        restaurant_uuid = UUID(restaurant_id)

        async with get_connection() as conn:
            rows = await conn.fetch(
                """SELECT id, period_start, period_end, status,
                          closed_by, closed_at, reopened_by, reopened_at, notes
                   FROM accounting_periods
                   WHERE restaurant_id = $1
                   ORDER BY period_start DESC""",
                restaurant_uuid,
            )

        return [
            {
                "period_id": str(r["id"]),
                "period_start": r["period_start"].isoformat(),
                "period_end": r["period_end"].isoformat(),
                "status": r["status"],
                "closed_by": r["closed_by"],
                "closed_at": r["closed_at"].isoformat() if r["closed_at"] else None,
                "reopened_by": r["reopened_by"],
                "reopened_at": r["reopened_at"].isoformat() if r["reopened_at"] else None,
                "notes": r["notes"],
            }
            for r in rows
        ]

    # ══════════════════════════════════════════════════════════════════════════
    # SHIFT CLOSE — Cash drawer → Ledger
    # ══════════════════════════════════════════════════════════════════════════

    async def record_shift_close(
        self,
        *,
        restaurant_id: str,
        branch_id: Optional[str],
        shift_id: str,
        cash_sales: float = 0,
        card_sales: float = 0,
        upi_sales: float = 0,
        created_by: str = "system",
    ) -> Optional[str]:
        """
        Shift close → DR Cash Drawer (Cash), DR Bank (UPI/Card), CR Revenue Summary.
        Reconciles physical cash drawer with ledger entries for the shift.
        Idempotent: one journal per shift.
        """
        lines = []
        total = Decimal("0")

        cash_amt = _quantize(cash_sales)
        card_amt = _quantize(card_sales)
        upi_amt = _quantize(upi_sales)

        if cash_amt > 0:
            lines.append({"account": "CASH", "debit": float(cash_amt), "credit": 0,
                          "description": "Cash drawer — shift close"})
            total += cash_amt
        if card_amt > 0:
            lines.append({"account": "CARD", "debit": float(card_amt), "credit": 0,
                          "description": "Card receipts — shift close"})
            total += card_amt
        if upi_amt > 0:
            lines.append({"account": "BANK", "debit": float(upi_amt), "credit": 0,
                          "description": "UPI receipts — shift close"})
            total += upi_amt

        if total <= 0:
            return None

        lines.append({"account": "FOOD_SALES", "debit": 0, "credit": float(total),
                       "description": f"Revenue summary — shift {shift_id}"})

        return await self.create_journal_entry(
            reference_type="shift_close",
            reference_id=shift_id,
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            description=f"Shift close {shift_id}: cash={cash_amt}, card={card_amt}, upi={upi_amt}",
            created_by=created_by,
            source_event="SHIFT_CLOSED",
            lines=lines,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # JOURNAL SEARCH — Query journal entries with filters
    # ══════════════════════════════════════════════════════════════════════════

    async def search_journals(
        self,
        restaurant_id: str,
        *,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        include_reversed: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """
        Search journal entries with optional filters.
        Returns entries with their lines.
        """
        from uuid import UUID
        restaurant_uuid = UUID(restaurant_id)
        if not from_date:
            from_date = date.today().replace(day=1)
        if not to_date:
            to_date = date.today()

        async with get_connection() as conn:
            # Build query with filters
            conditions = ["je.restaurant_id = $1", "je.entry_date BETWEEN $2 AND $3"]
            params: list = [restaurant_uuid, from_date, to_date]
            idx = 4

            if not include_reversed:
                conditions.append("je.is_reversed = false")

            if reference_type:
                conditions.append(f"je.reference_type = ${idx}")
                params.append(reference_type)
                idx += 1

            if reference_id:
                conditions.append(f"je.reference_id = ${idx}")
                params.append(reference_id)
                idx += 1

            where = " AND ".join(conditions)
            params.extend([limit, offset])

            entries = await conn.fetch(
                f"""
                SELECT je.id, je.entry_date, je.reference_type, je.reference_id,
                       je.description, je.is_reversed, je.reversed_by,
                       je.created_by, je.created_at, je.source_event
                FROM journal_entries je
                WHERE {where}
                ORDER BY je.created_at DESC
                LIMIT ${idx} OFFSET ${idx + 1}
                """,
                *params,
            )

            count = await conn.fetchval(
                f"SELECT COUNT(*) FROM journal_entries je WHERE {where}",
                *params[:-2],  # exclude limit/offset
            )

            # Fetch lines for each entry
            result = []
            for e in entries:
                lines = await conn.fetch(
                    """
                    SELECT jl.id, coa.account_code, coa.name AS account_name,
                           jl.debit, jl.credit, jl.description
                    FROM journal_lines jl
                    JOIN chart_of_accounts coa ON coa.id = jl.account_id
                    WHERE jl.journal_entry_id = $1
                    ORDER BY jl.debit DESC, jl.credit DESC
                    """,
                    e["id"],
                )
                result.append({
                    "id": str(e["id"]),
                    "entry_date": e["entry_date"].isoformat(),
                    "reference_type": e["reference_type"],
                    "reference_id": e["reference_id"],
                    "description": e["description"],
                    "is_reversed": e["is_reversed"],
                    "reversed_by": str(e["reversed_by"]) if e["reversed_by"] else None,
                    "created_by": e["created_by"],
                    "created_at": e["created_at"].isoformat(),
                    "source_event": e["source_event"],
                    "lines": [
                        {
                            "id": str(l["id"]),
                            "account_code": l["account_code"],
                            "account_name": l["account_name"],
                            "debit": float(l["debit"]),
                            "credit": float(l["credit"]),
                            "description": l["description"],
                        }
                        for l in lines
                    ],
                })

        return {
            "total": count,
            "entries": result,
            "limit": limit,
            "offset": offset,
        }

    # ══════════════════════════════════════════════════════════════════════════
    # INTEGRITY VALIDATOR
    # ══════════════════════════════════════════════════════════════════════════

    async def check_integrity(self, restaurant_id: str) -> dict:
        """
        Run full accounting integrity check via DB function.
        Returns pass/fail for: trial balance, entry balance, orphan lines,
        broken account refs, reversal linking.
        """
        restaurant_uuid = UUID(restaurant_id)
        async with get_connection() as conn:
            result = await conn.fetchval(
                "SELECT fn_check_accounting_integrity($1)",
                restaurant_uuid,
            )
        import json as _j
        return _j.loads(result) if isinstance(result, str) else dict(result) if result else {}

    async def drilldown(
        self,
        restaurant_id: str,
        *,
        account_id: Optional[str] = None,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        entry_date_from: Optional[date] = None,
        entry_date_to: Optional[date] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """
        Audit drilldown — trace any number back to journal entries + source documents.
        CA asks "where did this number come from?" → this answers it.
        Returns journal entries with lines AND source event references.
        """
        restaurant_uuid = UUID(restaurant_id)
        conditions = ["je.restaurant_id = $1"]
        params: list = [restaurant_uuid]
        idx = 2

        if account_id:
            conditions.append(f"jl.account_id = ${idx}")
            params.append(UUID(account_id))
            idx += 1

        if reference_type:
            conditions.append(f"je.reference_type = ${idx}")
            params.append(reference_type)
            idx += 1

        if reference_id:
            conditions.append(f"je.reference_id = ${idx}")
            params.append(reference_id)
            idx += 1

        if entry_date_from:
            conditions.append(f"je.entry_date >= ${idx}")
            params.append(entry_date_from)
            idx += 1

        if entry_date_to:
            conditions.append(f"je.entry_date <= ${idx}")
            params.append(entry_date_to)
            idx += 1

        where = " AND ".join(conditions)
        join_line = "JOIN journal_lines jl ON jl.journal_entry_id = je.id" if account_id else ""

        async with get_connection() as conn:
            entries = await conn.fetch(
                f"""SELECT DISTINCT je.id, je.entry_date, je.reference_type,
                           je.reference_id, je.description, je.is_reversed,
                           je.reversed_by, je.source_event, je.created_by,
                           je.created_at
                    FROM journal_entries je
                    {join_line}
                    WHERE {where}
                    ORDER BY je.entry_date DESC, je.created_at DESC
                    LIMIT {limit} OFFSET {offset}""",
                *params,
            )

            result = []
            for e in entries:
                lines = await conn.fetch(
                    """SELECT jl.id, coa.account_code, coa.name AS account_name,
                              coa.account_type, jl.debit, jl.credit, jl.description
                       FROM journal_lines jl
                       JOIN chart_of_accounts coa ON coa.id = jl.account_id
                       WHERE jl.journal_entry_id = $1
                       ORDER BY jl.debit DESC, jl.credit DESC""",
                    e["id"],
                )
                result.append({
                    "id": str(e["id"]),
                    "entry_date": e["entry_date"].isoformat(),
                    "reference_type": e["reference_type"],
                    "reference_id": e["reference_id"],
                    "description": e["description"],
                    "is_reversed": e["is_reversed"],
                    "reversed_by": str(e["reversed_by"]) if e["reversed_by"] else None,
                    "source_event": e["source_event"],
                    "created_by": e["created_by"],
                    "created_at": e["created_at"].isoformat(),
                    "lines": [
                        {
                            "id": str(l["id"]),
                            "account_code": l["account_code"],
                            "account_name": l["account_name"],
                            "account_type": l["account_type"],
                            "debit": float(l["debit"]),
                            "credit": float(l["credit"]),
                            "description": l["description"],
                        }
                        for l in lines
                    ],
                })

        return {"entries": result, "count": len(result), "limit": limit, "offset": offset}


# Singleton
accounting_engine = AccountingEngine()
