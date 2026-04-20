"""
Invoice Service — Invoice lifecycle management.

Bridges orders to accounting with proper tax/compliance structure.
Lifecycle: draft → issued → partially_paid → paid → cancelled/void

Usage:
    from app.services.invoice_service import invoice_service
    inv = await invoice_service.create_invoice(...)
    await invoice_service.record_payment(invoice_id, amount)
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


class InvoiceService:

    async def create_invoice(
        self,
        *,
        restaurant_id: str,
        branch_id: Optional[str] = None,
        customer_id: Optional[str] = None,
        customer_name: Optional[str] = None,
        customer_gstin: Optional[str] = None,
        order_id: Optional[str] = None,
        invoice_date: Optional[date] = None,
        due_date: Optional[date] = None,
        invoice_type: str = "tax_invoice",
        items: list[dict] = None,
        notes: str = "",
        terms: str = "",
        created_by: str = "system",
    ) -> dict:
        """
        Create a new invoice. Auto-generates invoice number.

        Items format:
        [{"item_name": str, "hsn_code": str, "quantity": float,
          "unit_price": float, "discount": float,
          "cgst_rate": float, "sgst_rate": float, "igst_rate": float}]
        """
        items = items or []

        async with get_serializable_transaction() as conn:
            # Generate invoice number: INV-YYYYMMDD-NNNN
            inv_date = invoice_date or date.today()
            prefix = f"INV-{inv_date.strftime('%Y%m%d')}"

            count = await conn.fetchval(
                """SELECT COUNT(*) FROM invoices
                   WHERE restaurant_id = $1 AND invoice_number LIKE $2""",
                UUID(restaurant_id), f"{prefix}%",
            )
            invoice_number = f"{prefix}-{count + 1:04d}"

            # Compute line totals
            subtotal = Decimal("0")
            total_cgst = Decimal("0")
            total_sgst = Decimal("0")
            total_igst = Decimal("0")
            total_discount = Decimal("0")
            line_rows = []

            for item in items:
                qty = _q(item.get("quantity", 1))
                price = _q(item.get("unit_price", 0))
                disc = _q(item.get("discount", 0))
                taxable = _q(qty * price - disc)
                cgst_rate = _q(item.get("cgst_rate", 0))
                sgst_rate = _q(item.get("sgst_rate", 0))
                igst_rate = _q(item.get("igst_rate", 0))
                cgst_amt = _q(taxable * cgst_rate / 100)
                sgst_amt = _q(taxable * sgst_rate / 100)
                igst_amt = _q(taxable * igst_rate / 100)
                line_total = _q(taxable + cgst_amt + sgst_amt + igst_amt)

                subtotal += taxable
                total_discount += disc
                total_cgst += cgst_amt
                total_sgst += sgst_amt
                total_igst += igst_amt

                line_rows.append({
                    "item_name": item["item_name"],
                    "hsn_code": item.get("hsn_code", ""),
                    "quantity": float(qty),
                    "unit_price": float(price),
                    "discount": float(disc),
                    "taxable_value": float(taxable),
                    "cgst_rate": float(cgst_rate),
                    "sgst_rate": float(sgst_rate),
                    "igst_rate": float(igst_rate),
                    "cgst_amount": float(cgst_amt),
                    "sgst_amount": float(sgst_amt),
                    "igst_amount": float(igst_amt),
                    "total": float(line_total),
                })

            total_amount = _q(subtotal + total_cgst + total_sgst + total_igst)

            # Insert invoice header
            inv_row = await conn.fetchrow(
                """INSERT INTO invoices
                    (restaurant_id, branch_id, invoice_number, invoice_date, due_date,
                     customer_id, customer_name, customer_gstin, order_id,
                     subtotal, discount_amount, cgst, sgst, igst,
                     total_amount, amount_paid, balance_due,
                     status, invoice_type, notes, terms, created_by)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,0,$15,
                        'issued',$16,$17,$18,$19)
                RETURNING id, invoice_number, total_amount, status""",
                UUID(restaurant_id),
                UUID(branch_id) if branch_id else None,
                invoice_number,
                inv_date,
                due_date,
                UUID(customer_id) if customer_id else None,
                customer_name, customer_gstin,
                UUID(order_id) if order_id else None,
                float(subtotal), float(total_discount),
                float(total_cgst), float(total_sgst), float(total_igst),
                float(total_amount),
                invoice_type, notes, terms, created_by,
            )
            invoice_id = inv_row["id"]

            # Insert line items
            for lr in line_rows:
                await conn.execute(
                    """INSERT INTO invoice_items
                        (invoice_id, item_name, hsn_code, quantity, unit_price,
                         discount, taxable_value,
                         cgst_rate, sgst_rate, igst_rate,
                         cgst_amount, sgst_amount, igst_amount, total)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)""",
                    invoice_id, lr["item_name"], lr["hsn_code"],
                    lr["quantity"], lr["unit_price"], lr["discount"],
                    lr["taxable_value"],
                    lr["cgst_rate"], lr["sgst_rate"], lr["igst_rate"],
                    lr["cgst_amount"], lr["sgst_amount"], lr["igst_amount"],
                    lr["total"],
                )

            # Post AR sub-ledger entry if customer exists
            if customer_id:
                from app.services.subledger_service import subledger_service
                from app.services.accounting_engine import accounting_engine

                journal_id = await accounting_engine.create_journal_entry(
                    reference_type="invoice",
                    reference_id=str(invoice_id),
                    restaurant_id=restaurant_id,
                    branch_id=branch_id,
                    description=f"Invoice {invoice_number}",
                    created_by=created_by,
                    entry_date=inv_date,
                    lines=[
                        {"account": "ACCOUNTS_RECEIVABLE", "debit": float(total_amount),
                         "credit": 0, "description": f"Invoice {invoice_number}"},
                        {"account": "FOOD_SALES", "debit": 0,
                         "credit": float(subtotal),
                         "description": f"Sales — {invoice_number}"},
                    ] + ([
                        {"account": "CGST_PAYABLE", "debit": 0,
                         "credit": float(total_cgst),
                         "description": "CGST on sales"}
                    ] if total_cgst > 0 else []) + ([
                        {"account": "SGST_PAYABLE", "debit": 0,
                         "credit": float(total_sgst),
                         "description": "SGST on sales"}
                    ] if total_sgst > 0 else []) + ([
                        {"account": "IGST_PAYABLE", "debit": 0,
                         "credit": float(total_igst),
                         "description": "IGST on sales"}
                    ] if total_igst > 0 else []),
                )

                if journal_id:
                    await conn.execute(
                        "UPDATE invoices SET journal_entry_id = $1 WHERE id = $2",
                        UUID(journal_id), invoice_id,
                    )
                    await subledger_service.post_customer_entry(
                        restaurant_id=restaurant_id,
                        customer_id=customer_id,
                        journal_entry_id=journal_id,
                        debit=float(total_amount),
                        reference_type="invoice",
                        reference_id=str(invoice_id),
                        description=f"Invoice {invoice_number}",
                        entry_date=inv_date,
                    )

        return {
            "id": str(invoice_id),
            "invoice_number": inv_row["invoice_number"],
            "total_amount": float(inv_row["total_amount"]),
            "status": inv_row["status"],
            "items_count": len(line_rows),
        }

    async def record_invoice_payment(
        self,
        *,
        invoice_id: str,
        restaurant_id: str,
        amount: float,
        payment_method: str = "cash",
        payment_id: Optional[str] = None,
        created_by: str = "system",
    ) -> dict:
        """
        Record a payment against an invoice. Updates status:
          issued → partially_paid → paid
        """
        amt = _q(amount)

        async with get_serializable_transaction() as conn:
            inv = await conn.fetchrow(
                """SELECT id, invoice_number, total_amount, amount_paid,
                       balance_due, status, customer_id
                FROM invoices
                WHERE id = $1 AND restaurant_id = $2
                FOR UPDATE""",
                UUID(invoice_id), UUID(restaurant_id),
            )
            if not inv:
                raise ValidationError("Invoice not found")
            if inv["status"] in ("paid", "cancelled", "void"):
                raise ValidationError(f"Invoice is {inv['status']}, cannot accept payment")

            new_paid = float(_q(inv["amount_paid"])) + float(amt)
            new_balance = float(_q(inv["total_amount"])) - new_paid
            new_status = "paid" if new_balance <= 0.005 else "partially_paid"

            await conn.execute(
                """UPDATE invoices
                   SET amount_paid = $1, balance_due = $2, status = $3,
                       updated_at = NOW()
                   WHERE id = $4""",
                round(new_paid, 2), max(round(new_balance, 2), 0),
                new_status, UUID(invoice_id),
            )

            # Post customer sub-ledger credit (payment received)
            if inv["customer_id"]:
                from app.services.subledger_service import subledger_service
                from app.services.accounting_engine import accounting_engine

                journal_id = await accounting_engine.record_payment(
                    restaurant_id=restaurant_id,
                    branch_id=None,
                    payment_id=payment_id or str(inv["id"]),
                    order_id=str(inv["id"]),
                    amount=float(amt),
                    method=payment_method,
                    created_by=created_by,
                )

                if journal_id:
                    await subledger_service.post_customer_entry(
                        restaurant_id=restaurant_id,
                        customer_id=str(inv["customer_id"]),
                        journal_entry_id=journal_id,
                        credit=float(amt),
                        reference_type="payment",
                        reference_id=payment_id or str(inv["id"]),
                        description=f"Payment for invoice {inv['invoice_number']}",
                    )

        return {
            "invoice_id": invoice_id,
            "amount_paid": round(new_paid, 2),
            "balance_due": max(round(new_balance, 2), 0),
            "status": new_status,
        }

    async def void_invoice(
        self,
        *,
        invoice_id: str,
        restaurant_id: str,
        reason: str = "",
        created_by: str = "system",
    ) -> dict:
        """Void an invoice. Reverses journal entry if exists."""
        async with get_serializable_transaction() as conn:
            inv = await conn.fetchrow(
                """SELECT id, invoice_number, status, journal_entry_id,
                       amount_paid, customer_id, total_amount
                FROM invoices WHERE id = $1 AND restaurant_id = $2
                FOR UPDATE""",
                UUID(invoice_id), UUID(restaurant_id),
            )
            if not inv:
                raise ValidationError("Invoice not found")
            if inv["status"] == "void":
                return {"invoice_id": invoice_id, "status": "void"}
            if float(inv["amount_paid"]) > 0:
                raise ValidationError("Cannot void invoice with payments. Refund first.")

            await conn.execute(
                """UPDATE invoices SET status = 'void', notes = COALESCE(notes,'') || $1,
                       updated_at = NOW()
                   WHERE id = $2""",
                f"\nVoided: {reason}" if reason else "\nVoided",
                UUID(invoice_id),
            )

            # Reverse journal entry
            if inv["journal_entry_id"]:
                from app.services.accounting_engine import accounting_engine
                await accounting_engine.reverse_entry(
                    entry_id=str(inv["journal_entry_id"]),
                    restaurant_id=restaurant_id,
                    reason=f"Void invoice {inv['invoice_number']}: {reason}",
                    created_by=created_by,
                )

                # Reverse sub-ledger
                if inv["customer_id"]:
                    from app.services.subledger_service import subledger_service
                    await subledger_service.post_customer_entry(
                        restaurant_id=restaurant_id,
                        customer_id=str(inv["customer_id"]),
                        journal_entry_id=str(inv["journal_entry_id"]),
                        credit=float(inv["total_amount"]),
                        reference_type="invoice_void",
                        reference_id=invoice_id,
                        description=f"Void invoice {inv['invoice_number']}",
                    )

        return {"invoice_id": invoice_id, "status": "void"}

    async def get_invoice(self, invoice_id: str, restaurant_id: str) -> dict:
        """Get invoice with line items."""
        async with get_connection() as conn:
            inv = await conn.fetchrow(
                """SELECT * FROM invoices
                   WHERE id = $1 AND restaurant_id = $2""",
                UUID(invoice_id), UUID(restaurant_id),
            )
            if not inv:
                raise ValidationError("Invoice not found")

            items = await conn.fetch(
                "SELECT * FROM invoice_items WHERE invoice_id = $1 ORDER BY created_at",
                UUID(invoice_id),
            )

        result = dict(inv)
        result["items"] = [dict(i) for i in items]
        return result

    async def list_invoices(
        self,
        restaurant_id: str,
        status: Optional[str] = None,
        customer_id: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List invoices with optional filters."""
        conditions = ["restaurant_id = $1"]
        params: list = [UUID(restaurant_id)]
        idx = 2

        if status:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1
        if customer_id:
            conditions.append(f"customer_id = ${idx}")
            params.append(UUID(customer_id))
            idx += 1
        if from_date:
            conditions.append(f"invoice_date >= ${idx}")
            params.append(from_date)
            idx += 1
        if to_date:
            conditions.append(f"invoice_date <= ${idx}")
            params.append(to_date)
            idx += 1

        where = " AND ".join(conditions)

        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""SELECT id, invoice_number, invoice_date, due_date,
                       customer_name, total_amount, amount_paid,
                       balance_due, status, invoice_type, created_at
                FROM invoices WHERE {where}
                ORDER BY invoice_date DESC, created_at DESC
                LIMIT ${idx} OFFSET ${idx+1}""",
                *params, limit, offset,
            )
        return [dict(r) for r in rows]

    async def get_unpaid_invoices(
        self, restaurant_id: str, customer_id: Optional[str] = None,
    ) -> list[dict]:
        """Get all invoices with outstanding balance."""
        conditions = ["restaurant_id = $1", "status IN ('issued','partially_paid')"]
        params: list = [UUID(restaurant_id)]
        idx = 2

        if customer_id:
            conditions.append(f"customer_id = ${idx}")
            params.append(UUID(customer_id))
            idx += 1

        where = " AND ".join(conditions)

        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""SELECT id, invoice_number, invoice_date, due_date,
                       customer_id, customer_name, total_amount,
                       amount_paid, balance_due, status
                FROM invoices WHERE {where}
                ORDER BY due_date ASC NULLS LAST, invoice_date ASC""",
                *params,
            )
        return [dict(r) for r in rows]


# Singleton
invoice_service = InvoiceService()
