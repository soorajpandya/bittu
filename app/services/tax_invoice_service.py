"""
Tax Invoice service — Phase 5 (platform → merchant tax invoices).

Owns the lifecycle of platform-issued tax invoices to merchants:

    draft ─issue→ issued ─cancel→ cancelled

Pure internal numbering — no external invoicing/payment-gateway integration.
Numbering: ``INV-{FY}-{MERCHANT4}-{NNNNN}`` allocated on issue (NOT on draft
create) so cancelled drafts don't burn invoice numbers.

NOTE: this is distinct from ``app.services.invoice_service`` which manages
merchant-facing customer invoices (orders → tax invoice). This service is
for invoices BITTU issues to merchants for platform fees / subscriptions.

Line-item math
──────────────
    taxable_amount = (quantity * unit_amount) - discount_amount
    cgst_amount    = round(taxable_amount * cgst_rate / 100, 4)   ... etc.
    line_total     = taxable_amount + cgst + sgst + igst + cess

Invoice header totals are recomputed from the line items on every line mutation.

Admin/merchant separation
─────────────────────────
    • Merchant: read own invoices, download/list — cannot create or issue.
    • Admin:    create draft, add lines, issue, cancel, list cross-merchant.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional
from uuid import UUID

from app.core.database import get_connection, get_transaction
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

Q4 = Decimal("0.0001")


def _f(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(round(v, 4))
    return float(v)


def _q(v) -> Decimal:
    return Decimal(str(v or 0))


def _round4(d: Decimal) -> Decimal:
    return d.quantize(Q4, rounding=ROUND_HALF_UP)


def _row_to_invoice(r) -> dict:
    if r is None:
        return {}
    md = r["metadata"]
    if isinstance(md, str):
        md = json.loads(md)
    return {
        "id":               str(r["id"]),
        "invoice_number":   r["invoice_number"],
        "merchant_id":      str(r["merchant_id"]),
        "branch_id":        str(r["branch_id"]) if r["branch_id"] else None,
        "invoice_date":     r["invoice_date"].isoformat() if r["invoice_date"] else None,
        "period_start":     r["period_start"].isoformat() if r["period_start"] else None,
        "period_end":       r["period_end"].isoformat() if r["period_end"] else None,
        "due_date":         r["due_date"].isoformat() if r["due_date"] else None,
        "currency":         r["currency"],
        "subtotal":         _f(r["subtotal"]),
        "cgst_total":       _f(r["cgst_total"]),
        "sgst_total":       _f(r["sgst_total"]),
        "igst_total":       _f(r["igst_total"]),
        "cess_total":       _f(r["cess_total"]),
        "discount_total":   _f(r["discount_total"]),
        "total_amount":     _f(r["total_amount"]),
        "place_of_supply":  r["place_of_supply"],
        "gstin_supplier":   r["gstin_supplier"],
        "gstin_customer":   r["gstin_customer"],
        "supplier_name":    r["supplier_name"],
        "supplier_address": r["supplier_address"],
        "customer_name":    r["customer_name"],
        "customer_address": r["customer_address"],
        "notes":            r["notes"],
        "status":           r["status"],
        "file_path":        r["file_path"],
        "metadata":         md or {},
        "created_at":       r["created_at"].isoformat(),
        "created_by":       str(r["created_by"]) if r["created_by"] else None,
        "issued_at":        r["issued_at"].isoformat() if r["issued_at"] else None,
        "issued_by":        str(r["issued_by"]) if r["issued_by"] else None,
        "cancelled_at":     r["cancelled_at"].isoformat() if r["cancelled_at"] else None,
        "cancelled_by":     str(r["cancelled_by"]) if r["cancelled_by"] else None,
        "cancellation_reason": r["cancellation_reason"],
        "updated_at":       r["updated_at"].isoformat(),
    }


def _row_to_line(r) -> dict:
    if r is None:
        return {}
    md = r["metadata"]
    if isinstance(md, str):
        md = json.loads(md)
    return {
        "id":              str(r["id"]),
        "invoice_id":      str(r["invoice_id"]),
        "sno":             r["sno"],
        "description":     r["description"],
        "hsn_sac":         r["hsn_sac"],
        "quantity":        _f(r["quantity"]),
        "unit_amount":     _f(r["unit_amount"]),
        "discount_amount": _f(r["discount_amount"]),
        "taxable_amount": _f(r["taxable_amount"]),
        "cgst_rate":       _f(r["cgst_rate"]),
        "cgst_amount":     _f(r["cgst_amount"]),
        "sgst_rate":       _f(r["sgst_rate"]),
        "sgst_amount":     _f(r["sgst_amount"]),
        "igst_rate":       _f(r["igst_rate"]),
        "igst_amount":     _f(r["igst_amount"]),
        "cess_rate":       _f(r["cess_rate"]),
        "cess_amount":     _f(r["cess_amount"]),
        "line_total":      _f(r["line_total"]),
        "metadata":        md or {},
        "created_at":      r["created_at"].isoformat(),
    }


class TaxInvoiceService:
    # ────────────────────────────────────────────────────────────────────
    # Create / mutate (admin only — routers enforce)
    # ────────────────────────────────────────────────────────────────────
    async def create_draft(
        self,
        *,
        merchant_id: str | UUID,
        branch_id: Optional[str | UUID] = None,
        invoice_date: Optional[date] = None,
        period_start: Optional[date] = None,
        period_end:   Optional[date] = None,
        due_date:     Optional[date] = None,
        currency: str = "INR",
        place_of_supply: Optional[str] = None,
        gstin_supplier: Optional[str] = None,
        gstin_customer: Optional[str] = None,
        supplier_name: Optional[str] = None,
        supplier_address: Optional[str] = None,
        customer_name: Optional[str] = None,
        customer_address: Optional[str] = None,
        notes: Optional[str] = None,
        metadata: Optional[dict] = None,
        created_by: Optional[str | UUID] = None,
    ) -> dict:
        if period_start and period_end and period_end < period_start:
            raise ValidationError("period_end must be >= period_start")
        async with get_transaction() as cx:
            row = await cx.fetchrow(
                """
                INSERT INTO tax_invoices
                    (invoice_number, merchant_id, branch_id, invoice_date,
                     period_start, period_end, due_date, currency, status,
                     place_of_supply, gstin_supplier, gstin_customer,
                     supplier_name, supplier_address,
                     customer_name, customer_address,
                     notes, metadata, created_by)
                VALUES (concat('DRAFT-', gen_random_uuid()::text),
                        $1::uuid, $2::uuid, COALESCE($3, (now() AT TIME ZONE 'UTC')::date),
                        $4, $5, $6, $7, 'draft',
                        $8, $9, $10,
                        $11, $12,
                        $13, $14,
                        $15, $16::jsonb, $17::uuid)
                RETURNING *
                """,
                str(merchant_id),
                str(branch_id) if branch_id else None,
                invoice_date,
                period_start, period_end, due_date,
                currency.upper(),
                place_of_supply, gstin_supplier, gstin_customer,
                supplier_name, supplier_address,
                customer_name, customer_address,
                notes, json.dumps(metadata or {}),
                str(created_by) if created_by else None,
            )
        return _row_to_invoice(row)

    async def add_line(
        self,
        *,
        invoice_id: str | UUID,
        description: str,
        hsn_sac: Optional[str] = None,
        quantity: float = 1,
        unit_amount: float = 0,
        discount_amount: float = 0,
        cgst_rate: float = 0,
        sgst_rate: float = 0,
        igst_rate: float = 0,
        cess_rate: float = 0,
        metadata: Optional[dict] = None,
    ) -> dict:
        if not description or len(description.strip()) < 1:
            raise ValidationError("description is required")
        qty   = _q(quantity)
        unit  = _q(unit_amount)
        disc  = _q(discount_amount)
        if qty <= 0:
            raise ValidationError("quantity must be > 0")
        if unit < 0 or disc < 0:
            raise ValidationError("unit_amount and discount_amount must be >= 0")
        cg = _q(cgst_rate); sg = _q(sgst_rate); ig = _q(igst_rate); ce = _q(cess_rate)
        if ig > 0 and (cg > 0 or sg > 0):
            raise ValidationError("IGST and CGST/SGST are mutually exclusive on a line")

        taxable = _round4(qty * unit - disc)
        if taxable < 0:
            raise ValidationError("discount_amount exceeds qty*unit_amount")
        cga = _round4(taxable * cg / 100)
        sga = _round4(taxable * sg / 100)
        iga = _round4(taxable * ig / 100)
        cea = _round4(taxable * ce / 100)
        line_total = _round4(taxable + cga + sga + iga + cea)

        async with get_transaction() as cx:
            inv = await cx.fetchrow(
                "SELECT id, status FROM tax_invoices WHERE id=$1::uuid FOR UPDATE",
                str(invoice_id),
            )
            if inv is None:
                raise NotFoundError("tax_invoice", str(invoice_id))
            if inv["status"] != "draft":
                raise ValidationError(
                    f"can only add lines while invoice is 'draft' (current: {inv['status']})"
                )
            sno = await cx.fetchval(
                "SELECT COALESCE(MAX(sno), 0) + 1 FROM tax_invoice_line_items "
                "WHERE invoice_id=$1::uuid",
                str(invoice_id),
            )
            line = await cx.fetchrow(
                """
                INSERT INTO tax_invoice_line_items
                    (invoice_id, sno, description, hsn_sac,
                     quantity, unit_amount, discount_amount, taxable_amount,
                     cgst_rate, cgst_amount, sgst_rate, sgst_amount,
                     igst_rate, igst_amount, cess_rate, cess_amount,
                     line_total, metadata)
                VALUES ($1::uuid, $2, $3, $4,
                        $5, $6, $7, $8,
                        $9, $10, $11, $12,
                        $13, $14, $15, $16,
                        $17, $18::jsonb)
                RETURNING *
                """,
                str(invoice_id), sno, description, hsn_sac,
                qty, unit, disc, taxable,
                cg, cga, sg, sga,
                ig, iga, ce, cea,
                line_total, json.dumps(metadata or {}),
            )
            await self._recompute_totals(cx, invoice_id)
        return _row_to_line(line)

    async def remove_line(
        self, *, invoice_id: str | UUID, line_id: str | UUID,
    ) -> dict:
        async with get_transaction() as cx:
            inv = await cx.fetchrow(
                "SELECT id, status FROM tax_invoices WHERE id=$1::uuid FOR UPDATE",
                str(invoice_id),
            )
            if inv is None:
                raise NotFoundError("tax_invoice", str(invoice_id))
            if inv["status"] != "draft":
                raise ValidationError(
                    f"can only remove lines while invoice is 'draft' (current: {inv['status']})"
                )
            deleted = await cx.execute(
                "DELETE FROM tax_invoice_line_items "
                "WHERE id=$1::uuid AND invoice_id=$2::uuid",
                str(line_id), str(invoice_id),
            )
            if deleted.endswith(" 0"):
                raise NotFoundError("tax_invoice_line_item", str(line_id))
            await self._recompute_totals(cx, invoice_id)
            inv_row = await cx.fetchrow(
                "SELECT * FROM tax_invoices WHERE id=$1::uuid", str(invoice_id),
            )
        return _row_to_invoice(inv_row)

    async def issue(
        self, *, invoice_id: str | UUID, actor_id: str | UUID,
    ) -> dict:
        async with get_transaction() as cx:
            inv = await cx.fetchrow(
                "SELECT * FROM tax_invoices WHERE id=$1::uuid FOR UPDATE",
                str(invoice_id),
            )
            if inv is None:
                raise NotFoundError("tax_invoice", str(invoice_id))
            if inv["status"] != "draft":
                raise ValidationError(
                    f"only 'draft' invoices can be issued (current: {inv['status']})"
                )
            n_lines = await cx.fetchval(
                "SELECT COUNT(*) FROM tax_invoice_line_items WHERE invoice_id=$1::uuid",
                str(invoice_id),
            )
            if not n_lines:
                raise ValidationError("cannot issue an invoice with zero line items")
            number = await cx.fetchval(
                "SELECT fn_next_invoice_number($1::uuid, $2)",
                str(inv["merchant_id"]), inv["invoice_date"],
            )
            updated = await cx.fetchrow(
                """
                UPDATE tax_invoices
                   SET invoice_number=$2, status='issued',
                       issued_at=now(), issued_by=$3::uuid
                 WHERE id=$1::uuid
             RETURNING *
                """,
                str(invoice_id), number, str(actor_id),
            )
        logger.info("tax_invoice.issued", extra={
            "invoice_id": str(invoice_id), "number": number,
        })
        return _row_to_invoice(updated)

    async def cancel(
        self,
        *,
        invoice_id: str | UUID,
        actor_id: str | UUID,
        reason: str,
    ) -> dict:
        if not reason or len(reason.strip()) < 3:
            raise ValidationError("cancellation reason is required (min 3 chars)")
        async with get_transaction() as cx:
            inv = await cx.fetchrow(
                "SELECT status FROM tax_invoices WHERE id=$1::uuid FOR UPDATE",
                str(invoice_id),
            )
            if inv is None:
                raise NotFoundError("tax_invoice", str(invoice_id))
            if inv["status"] not in ("draft", "issued"):
                raise ValidationError(
                    f"cannot cancel invoice in status {inv['status']!r}"
                )
            row = await cx.fetchrow(
                """
                UPDATE tax_invoices
                   SET status='cancelled', cancelled_at=now(),
                       cancelled_by=$2::uuid, cancellation_reason=$3
                 WHERE id=$1::uuid
             RETURNING *
                """,
                str(invoice_id), str(actor_id), reason,
            )
        return _row_to_invoice(row)

    # ────────────────────────────────────────────────────────────────────
    # Reads
    # ────────────────────────────────────────────────────────────────────
    async def list_invoices(
        self,
        *,
        merchant_id: Optional[str | UUID] = None,
        status: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date:   Optional[date] = None,
        limit: int = 50,
    ) -> list[dict]:
        clauses, params = [], []
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        if status:
            params.append(status)
            clauses.append(f"status = ${len(params)}::invoice_status")
        if from_date:
            params.append(from_date)
            clauses.append(f"invoice_date >= ${len(params)}")
        if to_date:
            params.append(to_date)
            clauses.append(f"invoice_date <= ${len(params)}")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(min(int(limit), 500))
        async with get_connection() as c:
            rows = await c.fetch(
                f"SELECT * FROM tax_invoices{where} "
                f"ORDER BY invoice_date DESC, created_at DESC LIMIT ${len(params)}",
                *params,
            )
        return [_row_to_invoice(r) for r in rows]

    async def get_invoice(
        self,
        *,
        invoice_id: str | UUID,
        merchant_id: Optional[str | UUID] = None,
        include_lines: bool = True,
    ) -> dict:
        clauses = ["id = $1::uuid"]
        params: list[Any] = [str(invoice_id)]
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        async with get_connection() as c:
            row = await c.fetchrow(
                f"SELECT * FROM tax_invoices WHERE {' AND '.join(clauses)}",
                *params,
            )
            if row is None:
                raise NotFoundError("tax_invoice", str(invoice_id))
            inv = _row_to_invoice(row)
            if include_lines:
                lines = await c.fetch(
                    "SELECT * FROM tax_invoice_line_items "
                    "WHERE invoice_id=$1::uuid ORDER BY sno",
                    str(invoice_id),
                )
                inv["line_items"] = [_row_to_line(l) for l in lines]
        return inv

    async def list_lines(self, invoice_id: str | UUID) -> list[dict]:
        async with get_connection() as c:
            rows = await c.fetch(
                "SELECT * FROM tax_invoice_line_items "
                "WHERE invoice_id=$1::uuid ORDER BY sno",
                str(invoice_id),
            )
        return [_row_to_line(r) for r in rows]

    # ────────────────────────────────────────────────────────────────────
    # CSV download
    # ────────────────────────────────────────────────────────────────────
    async def to_csv(
        self,
        *,
        invoice_id: str | UUID,
        merchant_id: Optional[str | UUID] = None,
    ) -> dict:
        inv = await self.get_invoice(
            invoice_id=invoice_id, merchant_id=merchant_id, include_lines=True,
        )
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Invoice", inv["invoice_number"]])
        w.writerow(["Date", inv["invoice_date"]])
        w.writerow(["Status", inv["status"]])
        w.writerow(["Currency", inv["currency"]])
        w.writerow(["Supplier", inv["supplier_name"] or "", inv["gstin_supplier"] or ""])
        w.writerow(["Customer", inv["customer_name"] or "", inv["gstin_customer"] or ""])
        w.writerow(["Place of Supply", inv["place_of_supply"] or ""])
        w.writerow([])
        w.writerow([
            "S.No", "Description", "HSN/SAC", "Qty", "Unit", "Discount",
            "Taxable", "CGST%", "CGST", "SGST%", "SGST",
            "IGST%", "IGST", "Cess%", "Cess", "Line Total",
        ])
        for li in inv["line_items"]:
            w.writerow([
                li["sno"], li["description"], li["hsn_sac"] or "",
                f"{li['quantity']:.4f}", f"{li['unit_amount']:.2f}",
                f"{li['discount_amount']:.2f}", f"{li['taxable_amount']:.2f}",
                f"{li['cgst_rate']:.2f}", f"{li['cgst_amount']:.2f}",
                f"{li['sgst_rate']:.2f}", f"{li['sgst_amount']:.2f}",
                f"{li['igst_rate']:.2f}", f"{li['igst_amount']:.2f}",
                f"{li['cess_rate']:.2f}", f"{li['cess_amount']:.2f}",
                f"{li['line_total']:.2f}",
            ])
        w.writerow([])
        w.writerow(["Subtotal", f"{inv['subtotal']:.2f}"])
        w.writerow(["CGST Total", f"{inv['cgst_total']:.2f}"])
        w.writerow(["SGST Total", f"{inv['sgst_total']:.2f}"])
        w.writerow(["IGST Total", f"{inv['igst_total']:.2f}"])
        w.writerow(["Cess Total", f"{inv['cess_total']:.2f}"])
        w.writerow(["Total", f"{inv['total_amount']:.2f}"])
        return {
            "invoice":      inv,
            "file_name":    f"{inv['invoice_number']}.csv",
            "file_content": buf.getvalue(),
        }

    # ────────────────────────────────────────────────────────────────────
    # Internal: recompute header totals from current line items
    # ────────────────────────────────────────────────────────────────────
    async def _recompute_totals(self, cx, invoice_id: str | UUID) -> None:
        sums = await cx.fetchrow(
            """
            SELECT COALESCE(SUM(taxable_amount), 0)  AS sub,
                   COALESCE(SUM(cgst_amount), 0)     AS cg,
                   COALESCE(SUM(sgst_amount), 0)     AS sg,
                   COALESCE(SUM(igst_amount), 0)     AS ig,
                   COALESCE(SUM(cess_amount), 0)     AS ce,
                   COALESCE(SUM(discount_amount), 0) AS dc,
                   COALESCE(SUM(line_total), 0)      AS tot
              FROM tax_invoice_line_items
             WHERE invoice_id = $1::uuid
            """,
            str(invoice_id),
        )
        await cx.execute(
            """
            UPDATE tax_invoices
               SET subtotal=$2, cgst_total=$3, sgst_total=$4,
                   igst_total=$5, cess_total=$6, discount_total=$7,
                   total_amount=$8
             WHERE id=$1::uuid
            """,
            str(invoice_id),
            sums["sub"], sums["cg"], sums["sg"],
            sums["ig"], sums["ce"], sums["dc"], sums["tot"],
        )


tax_invoice_service = TaxInvoiceService()
