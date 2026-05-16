"""
PDF invoice rendering for per-merchant outputs.

Two invoice types:

1. **Customer tax invoice** — what a diner/customer gets for an order.
   Pulled from `orders`/`order_items`/`restaurants` (merchant GST + address)
   and rendered with xhtml2pdf. Scoped strictly by `restaurant_id` to
   prevent cross-merchant leaks.

2. **SaaS invoice** — what Bittu bills the merchant each month for
   platform fees + GST. Pulled from `merchant_ledger` aggregates (the
   authoritative source for fee/gst deductions), persisted in
   `bittu_saas_invoices` (migration 063) so re-renders are idempotent.

We deliberately avoid Jinja to keep the dependency surface minimal —
xhtml2pdf ships in requirements.txt already.
"""
from __future__ import annotations

import html
import io
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from app.core.database import get_connection, get_service_connection
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

_ZERO = Decimal("0.00")


def _q(x) -> Decimal:
    """Quantize to 2dp banker-safe."""
    return Decimal(str(x or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _esc(value) -> str:
    """HTML-escape a value for safe interpolation into a template."""
    if value is None:
        return ""
    return html.escape(str(value))


def _money(value, *, currency: str = "INR") -> str:
    sym = "\u20B9" if currency.upper() == "INR" else currency + " "
    return f"{sym}{_q(value)}"


# ────────────────────────────────────────────────────────────────────
# HTML → PDF helper
# ────────────────────────────────────────────────────────────────────
def _html_to_pdf(html_doc: str) -> bytes:
    """Run xhtml2pdf on the given HTML; return raw PDF bytes."""
    try:
        from xhtml2pdf import pisa  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "xhtml2pdf is required for PDF rendering but failed to import"
        ) from exc

    buf = io.BytesIO()
    result = pisa.CreatePDF(src=html_doc, dest=buf, encoding="utf-8")
    if result.err:
        raise RuntimeError(f"xhtml2pdf reported {result.err} error(s) while rendering")
    return buf.getvalue()


# ────────────────────────────────────────────────────────────────────
# Customer tax invoice
# ────────────────────────────────────────────────────────────────────
async def render_customer_invoice(
    *,
    merchant_id: str,
    order_id: str,
) -> tuple[bytes, str]:
    """
    Build a customer-facing tax invoice for one order.

    Returns (pdf_bytes, suggested_filename). Raises NotFoundError if the
    order does not belong to ``merchant_id``.
    """
    async with get_connection() as conn:
        order = await conn.fetchrow(
            """
            SELECT o.id::text                     AS order_id,
                   o.created_at,
                   o.subtotal,
                   o.tax_amount,
                   o.discount_amount,
                   o.total_amount,
                   o.table_number,
                   o.notes,
                   c.name                         AS customer_name,
                   c.phone_number                 AS customer_phone,
                   r.name                         AS merchant_name,
                   r.address                      AS merchant_address,
                   r.city                         AS merchant_city,
                   r.state                        AS merchant_state,
                   r.pincode                      AS merchant_pincode,
                   r.gst_number                   AS merchant_gstin,
                   r.fssai_number                 AS merchant_fssai,
                   r.phone                        AS merchant_phone,
                   r.email                        AS merchant_email
            FROM orders o
            JOIN restaurants r ON r.id = o.restaurant_id
            LEFT JOIN customers c ON c.id = o.customer_id
            WHERE o.id = $1::uuid AND o.restaurant_id = $2::uuid
            """,
            str(order_id), str(merchant_id),
        )
        if order is None:
            raise NotFoundError("order not found for this merchant")

        items = await conn.fetch(
            """
            SELECT item_name, quantity, unit_price, total_price
            FROM order_items
            WHERE order_id = $1::uuid
            ORDER BY id
            """,
            str(order_id),
        )

    rows_html_parts: list[str] = []
    for i, it in enumerate(items, start=1):
        rows_html_parts.append(
            "<tr>"
            f"<td class='num'>{i}</td>"
            f"<td>{_esc(it['item_name'])}</td>"
            f"<td class='num'>{int(it['quantity'] or 0)}</td>"
            f"<td class='num'>{_money(it['unit_price'])}</td>"
            f"<td class='num'>{_money(it['total_price'])}</td>"
            "</tr>"
        )
    rows_html = "".join(rows_html_parts) or (
        "<tr><td colspan='5' class='center'>(no line items)</td></tr>"
    )

    addr_bits = [
        order["merchant_address"], order["merchant_city"],
        order["merchant_state"], order["merchant_pincode"],
    ]
    merchant_addr_full = ", ".join(_esc(b) for b in addr_bits if b)

    invoice_no = f"INV-{str(order['order_id'])[:8].upper()}"
    issued_on = (order["created_at"].date()
                 if order["created_at"] else date.today()).isoformat()

    html_doc = f"""
<!DOCTYPE html>
<html><head><meta charset='utf-8'/>
<style>
  @page {{ size: A4; margin: 1.5cm; }}
  body  {{ font-family: Helvetica, Arial, sans-serif; color: #222; font-size: 11pt; }}
  h1    {{ font-size: 18pt; margin: 0 0 4pt 0; }}
  .muted{{ color: #666; font-size: 9pt; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 10pt; }}
  th, td{{ border: 1px solid #ddd; padding: 6pt 8pt; text-align: left; }}
  th    {{ background: #f3f3f3; }}
  .num  {{ text-align: right; }}
  .center {{ text-align: center; }}
  .header {{ border-bottom: 2px solid #222; padding-bottom: 8pt; margin-bottom: 12pt; }}
  .totals td {{ border: none; padding: 3pt 8pt; }}
  .totals .label {{ text-align: right; color: #555; }}
  .totals .value {{ text-align: right; font-weight: bold; width: 30%; }}
  .grand {{ border-top: 2px solid #222 !important; font-size: 13pt; }}
</style></head>
<body>
  <div class='header'>
    <h1>{_esc(order['merchant_name'])}</h1>
    <div class='muted'>{merchant_addr_full}</div>
    <div class='muted'>
      Phone: {_esc(order['merchant_phone'])}
      &nbsp;|&nbsp; Email: {_esc(order['merchant_email'])}
    </div>
    <div class='muted'>
      GSTIN: {_esc(order['merchant_gstin'] or 'N/A')}
      &nbsp;|&nbsp; FSSAI: {_esc(order['merchant_fssai'] or 'N/A')}
    </div>
  </div>

  <table style='border:none; margin-top:0;'>
    <tr style='border:none;'>
      <td style='border:none; width:60%;'>
        <strong>Bill To:</strong><br/>
        {_esc(order['customer_name'] or 'Walk-in customer')}<br/>
        {_esc(order['customer_phone'] or '')}
      </td>
      <td style='border:none; text-align:right;'>
        <strong>Tax Invoice</strong><br/>
        Invoice #: {_esc(invoice_no)}<br/>
        Date: {_esc(issued_on)}<br/>
        Order: {_esc(str(order['order_id'])[:8].upper())}<br/>
        Table: {_esc(order['table_number'] or '-')}
      </td>
    </tr>
  </table>

  <table>
    <thead>
      <tr>
        <th style='width:5%;'>#</th>
        <th>Item</th>
        <th class='num' style='width:10%;'>Qty</th>
        <th class='num' style='width:15%;'>Unit</th>
        <th class='num' style='width:20%;'>Amount</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>

  <table class='totals' style='margin-top: 10pt;'>
    <tr><td class='label'>Subtotal</td><td class='value'>{_money(order['subtotal'])}</td></tr>
    <tr><td class='label'>Discount</td><td class='value'>- {_money(order['discount_amount'])}</td></tr>
    <tr><td class='label'>Tax (GST)</td><td class='value'>{_money(order['tax_amount'])}</td></tr>
    <tr class='grand'><td class='label'>Grand Total</td><td class='value'>{_money(order['total_amount'])}</td></tr>
  </table>

  <p class='muted' style='margin-top: 24pt; text-align: center;'>
    This is a computer-generated tax invoice. Thank you for dining with {_esc(order['merchant_name'])}.
  </p>
</body></html>
"""
    pdf_bytes = _html_to_pdf(html_doc)
    filename = f"invoice_{str(order['order_id'])[:8]}.pdf"
    return pdf_bytes, filename


# ────────────────────────────────────────────────────────────────────
# Bittu → Merchant SaaS invoice (monthly)
# ────────────────────────────────────────────────────────────────────
async def _aggregate_saas_period(
    *, merchant_id: str, year: int, month: int, currency: str = "INR",
) -> dict:
    """Aggregate fees/gst/gross from merchant_ledger for the period."""
    if not (1 <= month <= 12):
        raise ValidationError("month must be in 1..12")

    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT
              COALESCE(SUM(credit_amount)
                FILTER (WHERE transaction_type = 'payment_received'), 0)   AS gross_collected,
              COALESCE(SUM(debit_amount)
                FILTER (WHERE transaction_type = 'fee_deduction'), 0)      AS bittu_fee,
              COALESCE(SUM(debit_amount)
                FILTER (WHERE transaction_type = 'gst_deduction'), 0)      AS gst_on_fee,
              COUNT(*) FILTER (WHERE transaction_type = 'payment_received') AS txn_count
            FROM merchant_ledger
            WHERE merchant_id = $1::uuid
              AND currency    = $2
              AND created_at >= make_date($3::int, $4::int, 1)
              AND created_at <  (make_date($3::int, $4::int, 1) + INTERVAL '1 month')
            """,
            str(merchant_id), currency, int(year), int(month),
        )
    if row is None:
        return {
            "gross_collected": _ZERO, "bittu_fee": _ZERO, "gst_on_fee": _ZERO,
            "txn_count": 0,
        }
    return {
        "gross_collected": _q(row["gross_collected"]),
        "bittu_fee":       _q(row["bittu_fee"]),
        "gst_on_fee":      _q(row["gst_on_fee"]),
        "txn_count":       int(row["txn_count"] or 0),
    }


async def _persist_saas_invoice(
    *, merchant_id: str, year: int, month: int, currency: str,
    agg: dict,
) -> dict:
    """Upsert the SaaS-invoice ledger row. Idempotent on (merchant, year, month)."""
    invoice_number = f"BITTU-{int(year):04d}{int(month):02d}-{str(merchant_id)[:8].upper()}"
    total_payable = _q(agg["bittu_fee"] + agg["gst_on_fee"])

    async with get_service_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO bittu_saas_invoices (
              invoice_number, merchant_id, period_year, period_month,
              currency, gross_collected, bittu_fee_amount, gst_on_fee_amount,
              total_payable, txn_count, status
            ) VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, 'issued')
            ON CONFLICT (merchant_id, period_year, period_month) DO UPDATE
              SET gross_collected   = EXCLUDED.gross_collected,
                  bittu_fee_amount  = EXCLUDED.bittu_fee_amount,
                  gst_on_fee_amount = EXCLUDED.gst_on_fee_amount,
                  total_payable     = EXCLUDED.total_payable,
                  txn_count         = EXCLUDED.txn_count,
                  updated_at        = NOW()
            RETURNING *
            """,
            invoice_number, str(merchant_id), int(year), int(month),
            currency, agg["gross_collected"], agg["bittu_fee"],
            agg["gst_on_fee"], total_payable, agg["txn_count"],
        )
    return dict(row)


async def render_saas_invoice(
    *,
    merchant_id: str,
    year: int,
    month: int,
    currency: str = "INR",
) -> tuple[bytes, str]:
    """
    Generate (and persist) the Bittu→Merchant monthly SaaS invoice.
    Returns (pdf_bytes, suggested_filename).
    """
    agg = await _aggregate_saas_period(
        merchant_id=merchant_id, year=year, month=month, currency=currency,
    )
    inv = await _persist_saas_invoice(
        merchant_id=merchant_id, year=year, month=month,
        currency=currency, agg=agg,
    )

    # Fetch merchant name/GSTIN for the header
    async with get_connection() as conn:
        merch = await conn.fetchrow(
            "SELECT name, gst_number, address, city, state, pincode "
            "FROM restaurants WHERE id = $1::uuid",
            str(merchant_id),
        )
    if merch is None:
        raise NotFoundError("merchant restaurant row not found")

    addr_bits = [merch["address"], merch["city"], merch["state"], merch["pincode"]]
    merch_addr = ", ".join(_esc(b) for b in addr_bits if b)

    html_doc = f"""
<!DOCTYPE html>
<html><head><meta charset='utf-8'/>
<style>
  @page {{ size: A4; margin: 1.5cm; }}
  body  {{ font-family: Helvetica, Arial, sans-serif; color: #222; font-size: 11pt; }}
  h1    {{ font-size: 18pt; margin: 0 0 4pt 0; color: #1d4ed8; }}
  .muted{{ color: #666; font-size: 9pt; }}
  .header {{ border-bottom: 2px solid #1d4ed8; padding-bottom: 8pt; margin-bottom: 12pt; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 10pt; }}
  th, td{{ border: 1px solid #ddd; padding: 6pt 8pt; text-align: left; }}
  th    {{ background: #eff6ff; }}
  .num  {{ text-align: right; }}
  .totals td {{ border: none; padding: 3pt 8pt; }}
  .totals .label {{ text-align: right; color: #555; }}
  .totals .value {{ text-align: right; font-weight: bold; width: 30%; }}
  .grand {{ border-top: 2px solid #1d4ed8 !important; font-size: 13pt; color: #1d4ed8; }}
</style></head>
<body>
  <div class='header'>
    <h1>Bittu — Platform Services</h1>
    <div class='muted'>Monthly SaaS / Payment-Processing Invoice</div>
  </div>

  <table style='border:none; margin-top:0;'>
    <tr style='border:none;'>
      <td style='border:none; width:60%;'>
        <strong>Bill To:</strong><br/>
        {_esc(merch['name'])}<br/>
        {merch_addr}<br/>
        GSTIN: {_esc(merch['gst_number'] or 'N/A')}
      </td>
      <td style='border:none; text-align:right;'>
        <strong>Invoice #:</strong> {_esc(inv['invoice_number'])}<br/>
        <strong>Period:</strong> {int(month):02d}/{int(year):04d}<br/>
        <strong>Issued:</strong> {_esc(date.today().isoformat())}<br/>
        <strong>Currency:</strong> {_esc(currency)}
      </td>
    </tr>
  </table>

  <table>
    <thead>
      <tr><th>Description</th><th class='num' style='width:25%;'>Amount</th></tr>
    </thead>
    <tbody>
      <tr>
        <td>Gross collected via Bittu (informational)</td>
        <td class='num'>{_money(agg['gross_collected'])}</td>
      </tr>
      <tr>
        <td>Bittu platform fee ({int(agg['txn_count'])} transactions)</td>
        <td class='num'>{_money(agg['bittu_fee'])}</td>
      </tr>
      <tr>
        <td>GST on platform fee (18%)</td>
        <td class='num'>{_money(agg['gst_on_fee'])}</td>
      </tr>
    </tbody>
  </table>

  <table class='totals' style='margin-top: 10pt;'>
    <tr><td class='label'>Subtotal (fee + GST)</td>
        <td class='value'>{_money(inv['total_payable'])}</td></tr>
    <tr class='grand'><td class='label'>Total Payable</td>
        <td class='value'>{_money(inv['total_payable'])}</td></tr>
  </table>

  <p class='muted' style='margin-top: 24pt;'>
    Fees are auto-deducted via Razorpay Route at the time of each payment;
    this invoice is a consolidated tax document for accounting purposes.
    No further payment is required.
  </p>
</body></html>
"""
    pdf_bytes = _html_to_pdf(html_doc)
    filename = f"bittu_saas_invoice_{int(year):04d}_{int(month):02d}.pdf"
    return pdf_bytes, filename
