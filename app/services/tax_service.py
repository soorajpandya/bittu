"""
Tax Liability Service — GST lifecycle tracking.

Tracks: collected → payable → filed → paid
Computes net liability per period, records tax payments as journal entries.

Usage:
    from app.services.tax_service import tax_service
    report = await tax_service.compute_liability(restaurant_id, period_start, period_end)
    await tax_service.record_tax_payment(liability_id, ...)
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


class TaxLiabilityService:

    async def compute_liability(
        self,
        restaurant_id: str,
        period_start: date,
        period_end: date,
        branch_id: Optional[str] = None,
        period_label: Optional[str] = None,
        created_by: str = "system",
    ) -> dict:
        """
        Compute GST liability for a period by scanning journal lines.

        Output tax (collected) = credits to GST Payable accounts
        Input tax (on purchases) = debits to GST Payable accounts
        Net payable = collected - input
        """
        r_id = UUID(restaurant_id)

        async with get_serializable_transaction() as conn:
            # Check for existing liability for this period
            existing = await conn.fetchrow(
                """SELECT id, status FROM tax_liability
                   WHERE restaurant_id = $1 AND period_start = $2 AND period_end = $3""",
                r_id, period_start, period_end,
            )
            if existing and existing["status"] in ("filed", "paid"):
                raise ValidationError(
                    f"Tax liability for this period is already {existing['status']}"
                )

            # Compute output tax (CREDIT to GST payable = tax collected)
            output = await conn.fetchrow(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN coa.system_code = 'CGST_PAYABLE' THEN jl.credit END), 0) AS cgst_collected,
                    COALESCE(SUM(CASE WHEN coa.system_code = 'SGST_PAYABLE' THEN jl.credit END), 0) AS sgst_collected,
                    COALESCE(SUM(CASE WHEN coa.system_code = 'IGST_PAYABLE' THEN jl.credit END), 0) AS igst_collected
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND je.entry_date BETWEEN $2 AND $3
                  AND je.is_reversed = false
                  AND coa.system_code IN ('CGST_PAYABLE','SGST_PAYABLE','IGST_PAYABLE')
                  AND jl.credit > 0
                """,
                r_id, period_start, period_end,
            )

            # Compute input tax (DEBIT to GST payable = tax paid on purchases)
            input_tax = await conn.fetchrow(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN coa.system_code = 'CGST_PAYABLE' THEN jl.debit END), 0) AS cgst_input,
                    COALESCE(SUM(CASE WHEN coa.system_code = 'SGST_PAYABLE' THEN jl.debit END), 0) AS sgst_input,
                    COALESCE(SUM(CASE WHEN coa.system_code = 'IGST_PAYABLE' THEN jl.debit END), 0) AS igst_input
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND je.entry_date BETWEEN $2 AND $3
                  AND je.is_reversed = false
                  AND coa.system_code IN ('CGST_PAYABLE','SGST_PAYABLE','IGST_PAYABLE')
                  AND jl.debit > 0
                """,
                r_id, period_start, period_end,
            )

            cgst_collected = float(_q(output["cgst_collected"]))
            sgst_collected = float(_q(output["sgst_collected"]))
            igst_collected = float(_q(output["igst_collected"]))

            cgst_input = float(_q(input_tax["cgst_input"]))
            sgst_input = float(_q(input_tax["sgst_input"]))
            igst_input = float(_q(input_tax["igst_input"]))

            cgst_payable = round(cgst_collected - cgst_input, 2)
            sgst_payable = round(sgst_collected - sgst_input, 2)
            igst_payable = round(igst_collected - igst_input, 2)
            total_payable = round(cgst_payable + sgst_payable + igst_payable, 2)

            if existing:
                # Update existing
                await conn.execute(
                    """UPDATE tax_liability SET
                        cgst_collected=$1, sgst_collected=$2, igst_collected=$3,
                        cgst_input=$4, sgst_input=$5, igst_input=$6,
                        cgst_payable=$7, sgst_payable=$8, igst_payable=$9,
                        total_payable=$10, status='computed',
                        period_label=$11, updated_at=NOW()
                    WHERE id = $12""",
                    cgst_collected, sgst_collected, igst_collected,
                    cgst_input, sgst_input, igst_input,
                    cgst_payable, sgst_payable, igst_payable,
                    total_payable,
                    period_label or f"{period_start} to {period_end}",
                    existing["id"],
                )
                lid = str(existing["id"])
            else:
                row = await conn.fetchrow(
                    """INSERT INTO tax_liability
                        (restaurant_id, branch_id, period_start, period_end, period_label,
                         cgst_collected, sgst_collected, igst_collected,
                         cgst_input, sgst_input, igst_input,
                         cgst_payable, sgst_payable, igst_payable,
                         total_payable, status, created_by)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,'computed',$16)
                    RETURNING id""",
                    r_id,
                    UUID(branch_id) if branch_id else None,
                    period_start, period_end,
                    period_label or f"{period_start} to {period_end}",
                    cgst_collected, sgst_collected, igst_collected,
                    cgst_input, sgst_input, igst_input,
                    cgst_payable, sgst_payable, igst_payable,
                    total_payable, created_by,
                )
                lid = str(row["id"])

        return {
            "id": lid,
            "period": f"{period_start} to {period_end}",
            "cgst": {"collected": cgst_collected, "input": cgst_input, "payable": cgst_payable},
            "sgst": {"collected": sgst_collected, "input": sgst_input, "payable": sgst_payable},
            "igst": {"collected": igst_collected, "input": igst_input, "payable": igst_payable},
            "total_payable": total_payable,
            "status": "computed",
        }

    async def mark_filed(
        self,
        liability_id: str,
        restaurant_id: str,
    ) -> dict:
        """Mark a tax liability as filed with GST portal."""
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """UPDATE tax_liability
                   SET status = 'filed', filed_at = NOW(), updated_at = NOW()
                   WHERE id = $1 AND restaurant_id = $2
                     AND status IN ('computed', 'pending')
                   RETURNING id, status, total_payable""",
                UUID(liability_id), UUID(restaurant_id),
            )
            if not row:
                raise ValidationError("Liability not found or not in computed/pending status")
        return {"id": str(row["id"]), "status": "filed", "total_payable": float(row["total_payable"])}

    async def record_tax_payment(
        self,
        *,
        liability_id: str,
        restaurant_id: str,
        payment_method: str = "bank",
        payment_reference: str = "",
        created_by: str = "system",
    ) -> dict:
        """
        Record tax payment to government. Creates journal entry:
          DR CGST Payable, DR SGST Payable, DR IGST Payable
          CR Bank/Cash
        """
        async with get_serializable_transaction() as conn:
            tl = await conn.fetchrow(
                """SELECT * FROM tax_liability
                   WHERE id = $1 AND restaurant_id = $2
                   FOR UPDATE""",
                UUID(liability_id), UUID(restaurant_id),
            )
            if not tl:
                raise ValidationError("Tax liability not found")
            if tl["status"] == "paid":
                return {"id": str(tl["id"]), "status": "paid", "message": "Already paid"}
            if tl["total_payable"] <= 0:
                raise ValidationError("Nothing payable for this period")

            from app.services.accounting_engine import AccountingEngine, accounting_engine
            payment_account = AccountingEngine._payment_method_account(payment_method)

            lines = []
            if tl["cgst_payable"] > 0:
                lines.append({"account": "CGST_PAYABLE", "debit": float(tl["cgst_payable"]),
                               "credit": 0, "description": "CGST payment to govt"})
            if tl["sgst_payable"] > 0:
                lines.append({"account": "SGST_PAYABLE", "debit": float(tl["sgst_payable"]),
                               "credit": 0, "description": "SGST payment to govt"})
            if tl["igst_payable"] > 0:
                lines.append({"account": "IGST_PAYABLE", "debit": float(tl["igst_payable"]),
                               "credit": 0, "description": "IGST payment to govt"})
            lines.append({
                "account": payment_account, "debit": 0,
                "credit": float(tl["total_payable"]),
                "description": f"Tax payment — {tl['period_label'] or 'period'}",
            })

            journal_id = await accounting_engine.create_journal_entry(
                reference_type="tax_payment",
                reference_id=str(tl["id"]),
                restaurant_id=restaurant_id,
                description=f"GST payment for {tl['period_label'] or 'period'}",
                created_by=created_by,
                lines=lines,
            )

            await conn.execute(
                """UPDATE tax_liability
                   SET status = 'paid', paid_at = NOW(),
                       payment_reference = $1, payment_journal_id = $2,
                       updated_at = NOW()
                   WHERE id = $3""",
                payment_reference, UUID(journal_id) if journal_id else None,
                UUID(liability_id),
            )

        return {
            "id": liability_id,
            "status": "paid",
            "total_paid": float(tl["total_payable"]),
            "journal_entry_id": journal_id,
        }

    async def get_liability(self, liability_id: str, restaurant_id: str) -> dict:
        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tax_liability WHERE id = $1 AND restaurant_id = $2",
                UUID(liability_id), UUID(restaurant_id),
            )
            if not row:
                raise ValidationError("Tax liability not found")
        return dict(row)

    async def list_liabilities(
        self,
        restaurant_id: str,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        conditions = ["restaurant_id = $1"]
        params: list = [UUID(restaurant_id)]
        idx = 2

        if status:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1

        where = " AND ".join(conditions)

        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""SELECT id, period_start, period_end, period_label,
                       cgst_payable, sgst_payable, igst_payable,
                       total_payable, status, filed_at, paid_at, created_at
                FROM tax_liability WHERE {where}
                ORDER BY period_start DESC
                LIMIT ${idx} OFFSET ${idx+1}""",
                *params, limit, offset,
            )
        return [dict(r) for r in rows]

    async def gst_return_data(
        self,
        restaurant_id: str,
        period_start: date,
        period_end: date,
    ) -> dict:
        """
        Generate GSTR-3B summary data for a period.
        Includes outward supplies, inward supplies, and net tax.
        """
        r_id = UUID(restaurant_id)

        async with get_connection() as conn:
            # Outward supplies (sales)
            outward = await conn.fetchrow(
                """SELECT
                    COALESCE(SUM(subtotal), 0) AS taxable_value,
                    COALESCE(SUM(cgst), 0) AS cgst,
                    COALESCE(SUM(sgst), 0) AS sgst,
                    COALESCE(SUM(igst), 0) AS igst,
                    COALESCE(SUM(total_amount), 0) AS total,
                    COUNT(*) AS invoice_count
                FROM ar_invoices
                WHERE restaurant_id = $1
                  AND invoice_date BETWEEN $2 AND $3
                  AND status NOT IN ('draft', 'void', 'cancelled')""",
                r_id, period_start, period_end,
            )

            # Inward supplies (purchases with tax)
            inward = await conn.fetchrow(
                """SELECT
                    COALESCE(SUM(tax_amount), 0) AS total_input_tax,
                    COALESCE(SUM(total_amount), 0) AS total_purchases,
                    COUNT(*) AS expense_count
                FROM expenses
                WHERE restaurant_id = $1
                  AND expense_date BETWEEN $2 AND $3
                  AND tax_amount > 0""",
                r_id, period_start, period_end,
            )

            # Tax liability record
            liability = await conn.fetchrow(
                """SELECT * FROM tax_liability
                   WHERE restaurant_id = $1
                     AND period_start = $2 AND period_end = $3""",
                r_id, period_start, period_end,
            )

        return {
            "period": f"{period_start} to {period_end}",
            "outward_supplies": dict(outward) if outward else {},
            "inward_supplies": dict(inward) if inward else {},
            "liability": dict(liability) if liability else None,
        }


# Singleton
tax_service = TaxLiabilityService()
