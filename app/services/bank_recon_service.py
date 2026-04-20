"""
Bank Reconciliation Service.

Architecture
───────────────────────────────────────────────────────────────────────────────
Manages the bank reconciliation workflow:
  1. Import bank statement lines (CSV/manual entry)
  2. Auto-match statement lines to journal entries by amount + date
  3. Manual matching for unmatched items
  4. Reconciliation summary & reports

Matching strategy:
  - Auto-match: same amount, date within ±2 days, reference keyword match
  - Confidence scoring: 100 = exact match, 80+ = date mismatch, 60+ = fuzzy
  - All matches must be confirmed or manually created
───────────────────────────────────────────────────────────────────────────────
"""
import csv
import io
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from uuid import UUID

from app.core.database import get_connection, get_serializable_transaction
from app.core.exceptions import ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)


def _q(val) -> Decimal:
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class BankReconciliationService:

    # ══════════════════════════════════════════════════════════════════════
    # IMPORT BANK STATEMENTS
    # ══════════════════════════════════════════════════════════════════════

    async def import_statements_csv(
        self,
        *,
        restaurant_id: str,
        csv_content: str,
        bank_account: str = "",
        import_batch_id: Optional[str] = None,
    ) -> dict:
        """
        Import bank statement lines from CSV.
        Expected columns: date, description, reference, amount, balance
        Returns count of imported rows.
        """
        rid = UUID(restaurant_id)
        reader = csv.DictReader(io.StringIO(csv_content))

        imported = 0
        async with get_serializable_transaction() as conn:
            for row in reader:
                stmt_date = _parse_date(row.get("date", "").strip())
                if not stmt_date:
                    continue

                amount = _parse_amount(row.get("amount", "0").strip())
                balance = _parse_amount(row.get("balance", "").strip())
                desc = row.get("description", "").strip()
                ref = row.get("reference", "").strip()
                txn_type = row.get("type", "").strip()

                await conn.execute(
                    """INSERT INTO bank_statements
                           (restaurant_id, statement_date, description, reference,
                            amount, running_balance, bank_account, transaction_type,
                            import_batch_id)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                    rid, stmt_date, desc, ref,
                    float(amount), float(balance) if balance else None,
                    bank_account, txn_type or None,
                    import_batch_id,
                )
                imported += 1

        logger.info("bank_statements_imported", restaurant_id=restaurant_id, count=imported)
        return {"imported": imported, "batch_id": import_batch_id}

    async def add_statement_line(
        self,
        *,
        restaurant_id: str,
        statement_date: date,
        description: str,
        amount: float,
        reference: str = "",
        bank_account: str = "",
        transaction_type: str = "",
        value_date: Optional[date] = None,
    ) -> dict:
        """Add a single bank statement line manually."""
        rid = UUID(restaurant_id)
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """INSERT INTO bank_statements
                       (restaurant_id, statement_date, value_date, description,
                        reference, amount, bank_account, transaction_type)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   RETURNING id, statement_date, amount, status""",
                rid, statement_date, value_date, description,
                reference, amount, bank_account, transaction_type or None,
            )

        return {
            "id": str(row["id"]),
            "statement_date": row["statement_date"].isoformat(),
            "amount": float(row["amount"]),
            "status": row["status"],
        }

    # ══════════════════════════════════════════════════════════════════════
    # AUTO-MATCH
    # ══════════════════════════════════════════════════════════════════════

    async def auto_match(
        self,
        *,
        restaurant_id: str,
        date_tolerance_days: int = 2,
        matched_by: str = "system",
    ) -> dict:
        """
        Auto-match unmatched bank statement lines to journal entries.

        Strategy:
          1. For each unmatched statement line, find journal entries with:
             - Same restaurant
             - Amount matches (debit total or credit total on BANK/UPI account)
             - Date within ±tolerance days
             - Not already reconciled
          2. Score confidence based on date closeness + reference match
          3. Create reconciliation record
        """
        rid = UUID(restaurant_id)
        matched_count = 0
        total_unmatched = 0

        async with get_serializable_transaction() as conn:
            # Get all unmatched statement lines
            stmts = await conn.fetch(
                """SELECT id, statement_date, amount, reference, description
                   FROM bank_statements
                   WHERE restaurant_id = $1 AND status = 'unmatched'
                   ORDER BY statement_date""",
                rid,
            )
            total_unmatched = len(stmts)

            for stmt in stmts:
                stmt_amt = _q(stmt["amount"])
                stmt_date = stmt["statement_date"]
                date_lo = stmt_date - timedelta(days=date_tolerance_days)
                date_hi = stmt_date + timedelta(days=date_tolerance_days)

                # Find matching journal entries
                # Deposits (positive) match credit on BANK, withdrawals (negative) match debit
                if stmt_amt > 0:
                    # Bank deposit → look for journal entries crediting bank
                    candidates = await conn.fetch(
                        """SELECT je.id, je.entry_date, je.reference_type, je.reference_id,
                                  je.description,
                                  SUM(jl.credit) AS matched_amount
                           FROM journal_entries je
                           JOIN journal_lines jl ON jl.journal_entry_id = je.id
                           JOIN chart_of_accounts coa ON coa.id = jl.account_id
                           WHERE je.restaurant_id = $1
                             AND je.entry_date BETWEEN $2 AND $3
                             AND je.is_reversed = false
                             AND coa.system_code IN ('UPI_ACCOUNT', 'CASH_ACCOUNT', 'CARD_ACCOUNT')
                             AND jl.debit > 0
                             AND je.id NOT IN (
                                 SELECT journal_entry_id FROM bank_reconciliation
                                 WHERE restaurant_id = $1
                             )
                           GROUP BY je.id, je.entry_date, je.reference_type,
                                    je.reference_id, je.description
                           HAVING ABS(SUM(jl.debit) - $4) < 0.01""",
                        rid, date_lo, date_hi, float(abs(stmt_amt)),
                    )
                else:
                    # Bank withdrawal → look for journal entries debiting bank
                    candidates = await conn.fetch(
                        """SELECT je.id, je.entry_date, je.reference_type, je.reference_id,
                                  je.description,
                                  SUM(jl.credit) AS matched_amount
                           FROM journal_entries je
                           JOIN journal_lines jl ON jl.journal_entry_id = je.id
                           JOIN chart_of_accounts coa ON coa.id = jl.account_id
                           WHERE je.restaurant_id = $1
                             AND je.entry_date BETWEEN $2 AND $3
                             AND je.is_reversed = false
                             AND coa.system_code IN ('UPI_ACCOUNT', 'CASH_ACCOUNT', 'CARD_ACCOUNT')
                             AND jl.credit > 0
                             AND je.id NOT IN (
                                 SELECT journal_entry_id FROM bank_reconciliation
                                 WHERE restaurant_id = $1
                             )
                           GROUP BY je.id, je.entry_date, je.reference_type,
                                    je.reference_id, je.description
                           HAVING ABS(SUM(jl.credit) - $4) < 0.01""",
                        rid, date_lo, date_hi, float(abs(stmt_amt)),
                    )

                if not candidates:
                    continue

                # Pick best candidate (closest date, reference match)
                best = None
                best_score = 0
                stmt_ref = (stmt["reference"] or "").lower()
                stmt_desc = (stmt["description"] or "").lower()

                for c in candidates:
                    score = 60.0  # base score for amount match
                    # Date closeness bonus (max 20 pts)
                    day_diff = abs((c["entry_date"] - stmt_date).days)
                    score += max(0, 20 - (day_diff * 10))
                    # Reference match bonus (max 20 pts)
                    c_ref = (c["reference_id"] or "").lower()
                    c_desc = (c["description"] or "").lower()
                    if stmt_ref and (stmt_ref in c_ref or stmt_ref in c_desc):
                        score += 20
                    elif stmt_desc and (c_ref in stmt_desc or c_desc in stmt_desc):
                        score += 10

                    if score > best_score:
                        best_score = score
                        best = c

                if best and best_score >= 60:
                    # Create reconciliation record
                    await conn.execute(
                        """INSERT INTO bank_reconciliation
                               (restaurant_id, bank_statement_id, journal_entry_id,
                                match_type, match_confidence, matched_by)
                           VALUES ($1, $2, $3, 'auto', $4, $5)""",
                        rid, stmt["id"], best["id"],
                        best_score, matched_by,
                    )
                    # Mark statement as matched
                    await conn.execute(
                        "UPDATE bank_statements SET status = 'matched', matched_at = NOW() WHERE id = $1",
                        stmt["id"],
                    )
                    matched_count += 1

        logger.info(
            "bank_auto_match_complete",
            restaurant_id=restaurant_id,
            matched=matched_count,
            total_unmatched=total_unmatched,
        )
        return {
            "matched": matched_count,
            "unmatched_remaining": total_unmatched - matched_count,
        }

    # ══════════════════════════════════════════════════════════════════════
    # MANUAL MATCH
    # ══════════════════════════════════════════════════════════════════════

    async def manual_match(
        self,
        *,
        restaurant_id: str,
        bank_statement_id: str,
        journal_entry_id: str,
        matched_by: str,
        notes: str = "",
    ) -> dict:
        """Manually match a bank statement line to a journal entry."""
        rid = UUID(restaurant_id)
        stmt_uuid = UUID(bank_statement_id)
        je_uuid = UUID(journal_entry_id)

        async with get_serializable_transaction() as conn:
            # Verify statement belongs to restaurant and is unmatched
            stmt = await conn.fetchrow(
                "SELECT id, status FROM bank_statements WHERE id = $1 AND restaurant_id = $2",
                stmt_uuid, rid,
            )
            if not stmt:
                raise ValidationError("Bank statement not found")
            if stmt["status"] == "matched":
                raise ValidationError("Statement line is already matched")

            # Verify journal entry belongs to restaurant
            je = await conn.fetchrow(
                "SELECT id FROM journal_entries WHERE id = $1 AND restaurant_id = $2",
                je_uuid, rid,
            )
            if not je:
                raise ValidationError("Journal entry not found")

            # Create reconciliation
            await conn.execute(
                """INSERT INTO bank_reconciliation
                       (restaurant_id, bank_statement_id, journal_entry_id,
                        match_type, match_confidence, matched_by, notes)
                   VALUES ($1, $2, $3, 'manual', 100, $4, $5)
                   ON CONFLICT (bank_statement_id, journal_entry_id) DO NOTHING""",
                rid, stmt_uuid, je_uuid, matched_by, notes,
            )

            # Mark statement as matched
            await conn.execute(
                "UPDATE bank_statements SET status = 'matched', matched_at = NOW() WHERE id = $1",
                stmt_uuid,
            )

        return {"status": "matched", "bank_statement_id": bank_statement_id, "journal_entry_id": journal_entry_id}

    async def unmatch(
        self,
        *,
        restaurant_id: str,
        bank_statement_id: str,
        journal_entry_id: str,
    ) -> dict:
        """Remove a reconciliation match."""
        rid = UUID(restaurant_id)
        stmt_uuid = UUID(bank_statement_id)
        je_uuid = UUID(journal_entry_id)

        async with get_serializable_transaction() as conn:
            deleted = await conn.execute(
                "DELETE FROM bank_reconciliation WHERE restaurant_id = $1 "
                "AND bank_statement_id = $2 AND journal_entry_id = $3",
                rid, stmt_uuid, je_uuid,
            )

            # Check if statement has any other matches
            remaining = await conn.fetchval(
                "SELECT COUNT(*) FROM bank_reconciliation WHERE bank_statement_id = $1",
                stmt_uuid,
            )
            if remaining == 0:
                await conn.execute(
                    "UPDATE bank_statements SET status = 'unmatched', matched_at = NULL WHERE id = $1",
                    stmt_uuid,
                )

        return {"status": "unmatched", "bank_statement_id": bank_statement_id}

    async def exclude_statement(
        self,
        *,
        restaurant_id: str,
        bank_statement_id: str,
    ) -> dict:
        """Exclude a statement line from reconciliation (e.g. bank charges)."""
        rid = UUID(restaurant_id)
        stmt_uuid = UUID(bank_statement_id)

        async with get_connection() as conn:
            await conn.execute(
                "UPDATE bank_statements SET status = 'excluded' "
                "WHERE id = $1 AND restaurant_id = $2",
                stmt_uuid, rid,
            )

        return {"status": "excluded", "bank_statement_id": bank_statement_id}

    # ══════════════════════════════════════════════════════════════════════
    # QUERIES
    # ══════════════════════════════════════════════════════════════════════

    async def list_statements(
        self,
        restaurant_id: str,
        status: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """List bank statement lines with optional filters."""
        rid = UUID(restaurant_id)
        params: list = [rid]
        clauses = ["restaurant_id = $1"]

        if status:
            params.append(status)
            clauses.append(f"status = ${len(params)}")
        if from_date:
            params.append(from_date)
            clauses.append(f"statement_date >= ${len(params)}")
        if to_date:
            params.append(to_date)
            clauses.append(f"statement_date <= ${len(params)}")

        params.extend([limit, offset])
        where = " AND ".join(clauses)

        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""SELECT bs.*, br.journal_entry_id, br.match_type, br.match_confidence
                    FROM bank_statements bs
                    LEFT JOIN bank_reconciliation br ON br.bank_statement_id = bs.id
                    WHERE {where}
                    ORDER BY bs.statement_date DESC
                    LIMIT ${len(params) - 1} OFFSET ${len(params)}""",
                *params,
            )

        return [
            {
                "id": str(r["id"]),
                "statement_date": r["statement_date"].isoformat(),
                "value_date": r["value_date"].isoformat() if r["value_date"] else None,
                "description": r["description"],
                "reference": r["reference"],
                "amount": float(r["amount"]),
                "running_balance": float(r["running_balance"]) if r["running_balance"] else None,
                "bank_account": r["bank_account"],
                "transaction_type": r["transaction_type"],
                "status": r["status"],
                "matched_at": r["matched_at"].isoformat() if r["matched_at"] else None,
                "journal_entry_id": str(r["journal_entry_id"]) if r["journal_entry_id"] else None,
                "match_type": r["match_type"],
                "match_confidence": float(r["match_confidence"]) if r["match_confidence"] else None,
            }
            for r in rows
        ]

    async def reconciliation_summary(
        self,
        restaurant_id: str,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
    ) -> dict:
        """
        Reconciliation summary: matched vs unmatched counts + amounts.
        This is the key report for bank reconciliation.
        """
        rid = UUID(restaurant_id)
        params: list = [rid]
        date_clause = ""

        if from_date:
            params.append(from_date)
            date_clause += f" AND statement_date >= ${len(params)}"
        if to_date:
            params.append(to_date)
            date_clause += f" AND statement_date <= ${len(params)}"

        async with get_connection() as conn:
            summary = await conn.fetchrow(
                f"""SELECT
                        COUNT(*) FILTER (WHERE status = 'matched') AS matched_count,
                        COUNT(*) FILTER (WHERE status = 'unmatched') AS unmatched_count,
                        COUNT(*) FILTER (WHERE status = 'excluded') AS excluded_count,
                        COALESCE(SUM(amount) FILTER (WHERE status = 'matched'), 0) AS matched_amount,
                        COALESCE(SUM(amount) FILTER (WHERE status = 'unmatched'), 0) AS unmatched_amount,
                        COALESCE(SUM(amount) FILTER (WHERE status = 'excluded'), 0) AS excluded_amount,
                        COUNT(*) AS total_count,
                        COALESCE(SUM(amount), 0) AS total_amount
                    FROM bank_statements
                    WHERE restaurant_id = $1{date_clause}""",
                *params,
            )

            # Get ledger balance for comparison
            ledger_balance = await conn.fetchval(
                """SELECT COALESCE(SUM(jl.debit - jl.credit), 0)
                   FROM journal_lines jl
                   JOIN journal_entries je ON je.id = jl.journal_entry_id
                   JOIN chart_of_accounts coa ON coa.id = jl.account_id
                   WHERE je.restaurant_id = $1
                     AND je.is_reversed = false
                     AND coa.system_code IN ('UPI_ACCOUNT', 'CASH_ACCOUNT', 'CARD_ACCOUNT')""",
                rid,
            )

        return {
            "matched_count": summary["matched_count"],
            "unmatched_count": summary["unmatched_count"],
            "excluded_count": summary["excluded_count"],
            "matched_amount": float(summary["matched_amount"]),
            "unmatched_amount": float(summary["unmatched_amount"]),
            "excluded_amount": float(summary["excluded_amount"]),
            "total_statements": summary["total_count"],
            "total_statement_amount": float(summary["total_amount"]),
            "ledger_balance": float(ledger_balance or 0),
            "difference": float((summary["total_amount"] or 0) - (ledger_balance or 0)),
        }


# ── Module-level singleton ───────────────────────────────────────────────────
bank_recon_service = BankReconciliationService()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> Optional[date]:
    """Parse date from common formats."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(s: str) -> Decimal:
    """Parse amount, handling commas and currency symbols."""
    if not s:
        return Decimal("0")
    cleaned = s.replace(",", "").replace("₹", "").replace("$", "").strip()
    try:
        return _q(cleaned)
    except Exception:
        return Decimal("0")
