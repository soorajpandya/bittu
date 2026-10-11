"""
PDF invoice rendering for per-merchant outputs.

Two invoice types:

1. **Customer tax invoice** — what a diner/customer gets for an order.
   Pulled from `orders`/`order_items`/`restaurants` (merchant GST + address)
   and rendered with xhtml2pdf. Scoped strictly by `restaurant_id` to
   prevent cross-merchant leaks.

2. **SaaS invoice** — what Bittu bills the merchant each month for
   platform fees + GST. Computed from the Razorpay Route mirror (frozen
   per-payment allocations, D1 full-consideration GST basis), persisted in
   `bittu_saas_invoices` (migrations 063/090); frozen months are never
   rewritten — late refunds become `bittu_saas_credit_notes`.

We deliberately avoid Jinja to keep the dependency surface minimal —
xhtml2pdf ships in requirements.txt already.
"""
from __future__ import annotations

import html
import io
import json
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
                   o.cgst_amount,
                   o.sgst_amount,
                   o.gst_number                   AS order_gst_number,
                   o.round_off,
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
            SELECT item_name, quantity, unit_price, total_price,
                   gst_enabled, gst_inclusive, gst_rate,
                   taxable_amount, cgst_amount, sgst_amount
            FROM order_items
            WHERE order_id = $1::uuid
            ORDER BY id
            """,
            str(order_id),
        )

    rows_html_parts: list[str] = []
    inclusive_subtotal = Decimal("0")
    exclusive_subtotal = Decimal("0")
    nongst_subtotal    = Decimal("0")
    for i, it in enumerate(items, start=1):
        line_total = Decimal(str(it["total_price"] or 0))
        gst_on   = bool(it["gst_enabled"])
        incl     = bool(it["gst_inclusive"])
        tag = ""
        if gst_on and incl:
            tag = " <span class='muted'>(Incl. GST)</span>"
            inclusive_subtotal += line_total
        elif gst_on:
            exclusive_subtotal += line_total
        else:
            nongst_subtotal += line_total
        rows_html_parts.append(
            "<tr>"
            f"<td class='num'>{i}</td>"
            f"<td>{_esc(it['item_name'])}{tag}</td>"
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

    # ── Tax-row rendering ────────────────────────────────────────
    # Show CGST + SGST split when the order was billed with GST.
    # When GST was disabled at billing time, omit the tax rows entirely.
    cgst_amt = order["cgst_amount"] or Decimal("0")
    sgst_amt = order["sgst_amount"] or Decimal("0")
    tax_amt  = order["tax_amount"]  or Decimal("0")

    # Rate labels come from the persisted per-line snapshot, never recomputed
    # from amounts (mixed inclusive/exclusive carts have no single effective rate).
    rates = {Decimal(str(it["gst_rate"])) for it in items if it["gst_enabled"] and it["gst_rate"] is not None}

    def _pct(_amount: Decimal) -> str:
        if len(rates) != 1:
            return ""
        return f"{(next(iter(rates)) / 2).quantize(Decimal('0.01')).normalize()}%"

    tax_rows_html = ""
    if cgst_amt or sgst_amt:
        tax_rows_html = (
            f"<tr><td class='label'>CGST {_pct(cgst_amt)}</td>"
            f"<td class='value'>{_money(cgst_amt)}</td></tr>"
            f"<tr><td class='label'>SGST {_pct(sgst_amt)}</td>"
            f"<td class='value'>{_money(sgst_amt)}</td></tr>"
        )
    elif tax_amt:
        # Legacy row (pre-M065) without CGST/SGST split persisted.
        tax_rows_html = (
            f"<tr><td class='label'>Tax (GST)</td>"
            f"<td class='value'>{_money(tax_amt)}</td></tr>"
        )

    round_off_html = ""
    if order["round_off"] and Decimal(str(order["round_off"])) != 0:
        round_off_html = (
            f"<tr><td class='label'>Round off</td>"
            f"<td class='value'>{_money(order['round_off'])}</td></tr>"
        )

    discount_html = ""
    if order["discount_amount"] and Decimal(str(order["discount_amount"])) > 0:
        discount_html = (
            f"<tr><td class='label'>Discount</td>"
            f"<td class='value'>- {_money(order['discount_amount'])}</td></tr>"
        )

    # Per-item GST mix breakdown (M066). Only render rows where the
    # bucket actually has items so the totals block stays compact for
    # the common single-mode case.
    split_parts: list[str] = []
    if inclusive_subtotal > 0:
        split_parts.append(
            f"<tr><td class='label muted'>&nbsp;&nbsp;Items with GST included in price</td>"
            f"<td class='value muted'>{_money(inclusive_subtotal)}</td></tr>"
        )
    if exclusive_subtotal > 0:
        split_parts.append(
            f"<tr><td class='label muted'>&nbsp;&nbsp;Items taxed on top</td>"
            f"<td class='value muted'>{_money(exclusive_subtotal)}</td></tr>"
        )
    if nongst_subtotal > 0:
        split_parts.append(
            f"<tr><td class='label muted'>&nbsp;&nbsp;Non-GST items</td>"
            f"<td class='value muted'>{_money(nongst_subtotal)}</td></tr>"
        )
    # Only show split when more than one bucket is in play \u2014 single-mode
    # carts already look clean with just Subtotal + tax rows.
    subtotal_split_html = "".join(split_parts) if sum(
        1 for v in (inclusive_subtotal, exclusive_subtotal, nongst_subtotal) if v > 0
    ) > 1 else ""

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
      GSTIN: {_esc(order['order_gst_number'] or order['merchant_gstin'] or 'N/A')}
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
    {subtotal_split_html}
    {discount_html}
    {tax_rows_html}
    {round_off_html}
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
# Bittu → Merchant SaaS invoice (monthly) — sourced from the Route mirror
# ────────────────────────────────────────────────────────────────────
#
# Basis (D1, "full_consideration"): for every captured gateway payment in the
# IST month with a frozen Route allocation, the taxable supply is the whole
# deduction (gross − merchant entitlement), split 4.2373% fee + 18% GST with
# single HALF_UP paise rounding per payment (fee_policy.gst_split_paise), so
# invoice fee + GST == Σ ledger fee/gst legs == Σ total_deduction exactly.
#
# Refunds processed in the month credit back fee + GST on the refunded
# proportion (same cumulative FLOOR rule as Route reversals). A month is
# frozen ('issued') on first render after it ends if every payment has a
# resolved allocation; later refunds against a frozen month become credit
# notes. Issued amounts are never rewritten.

from calendar import monthrange

from app.services.razorpay.fee_policy import gst_split_paise, proportional_share_paise
from app.services.razorpay.finance_queries import PAYMENTS_CTE

_IST = "Asia/Kolkata"


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    if not (1 <= month <= 12):
        raise ValidationError("month must be in 1..12")
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def _ist_today() -> date:
    from zoneinfo import ZoneInfo
    from datetime import datetime
    return datetime.now(ZoneInfo(_IST)).date()


_REFUND_CREDITS_SQL = """
WITH refunds AS (
    SELECT r.refund_id, r.razorpay_payment_id, r.amount_paise,
           COALESCE(r.processed_at, r.created_at) AS processed_at,
           p.amount_paise AS gross_paise,
           COALESCE(a.merchant_paise, t.merchant_paise) AS merchant_paise,
           SUM(r.amount_paise) OVER (PARTITION BY r.razorpay_payment_id
               ORDER BY COALESCE(r.processed_at, r.created_at), r.refund_id) AS cumulative
    FROM rzp_refunds r
    JOIN rzp_payments p ON p.razorpay_payment_id = r.razorpay_payment_id AND p.merchant_id = r.merchant_id
    LEFT JOIN rzp_route_allocations a ON a.razorpay_payment_id = r.razorpay_payment_id AND a.merchant_id = r.merchant_id
    LEFT JOIN LATERAL (
        SELECT SUM(amount_paise)::bigint AS merchant_paise FROM rzp_route_transfers t
        WHERE t.razorpay_payment_id = r.razorpay_payment_id AND t.merchant_id = r.merchant_id AND t.status::text <> 'failed'
    ) t ON true
    WHERE r.merchant_id = $1::uuid AND r.status = 'processed' AND p.captured = true AND p.currency = $4
)
SELECT * FROM refunds
WHERE (processed_at AT TIME ZONE 'Asia/Kolkata')::date BETWEEN $2 AND $3
ORDER BY processed_at, refund_id
"""


async def _aggregate_saas_period(
    *, merchant_id: str, year: int, month: int, currency: str = "INR",
) -> dict:
    """Aggregate the month from the Razorpay mirror (online rows only)."""
    first, last = _month_bounds(year, month)
    gross = deduction = fee = gst = 0
    txn_count = unresolved = 0
    refund_lines: list[dict] = []
    refund_fee = refund_gst = refund_gross = 0

    async with get_connection() as conn:
        rows = await conn.fetch(
            PAYMENTS_CTE + "SELECT amount_paise, merchant_paise, payment_id FROM payment_rows WHERE source='razorpay' AND currency=$5",
            str(merchant_id), first, last, None, currency.upper(),
        )
        for r in rows:
            txn_count += 1
            gross += int(r["amount_paise"])
            if r["merchant_paise"] is None:
                unresolved += 1
                continue
            d = int(r["amount_paise"]) - int(r["merchant_paise"])
            f, g = gst_split_paise(d)
            deduction += d
            fee += f
            gst += g

        for r in await conn.fetch(_REFUND_CREDITS_SQL, str(merchant_id), first, last, currency.upper()):
            if r["merchant_paise"] is None:
                unresolved += 1
                continue
            gross_p, merchant_p = int(r["gross_paise"]), int(r["merchant_paise"])
            amount, cumulative = int(r["amount_paise"]), int(r["cumulative"])
            merchant_share = proportional_share_paise(
                cumulative_paise=cumulative, previous_cumulative_paise=cumulative - amount,
                gross_paise=gross_p, share_paise=merchant_p,
            )
            fee_total, _ = gst_split_paise(gross_p - merchant_p)
            fee_back = proportional_share_paise(
                cumulative_paise=cumulative, previous_cumulative_paise=cumulative - amount,
                gross_paise=gross_p, share_paise=fee_total,
            )
            gst_back = max(amount - merchant_share - fee_back, 0)
            refund_lines.append({
                "refund_id": r["refund_id"], "razorpay_payment_id": r["razorpay_payment_id"],
                "recovered_paise": amount, "fee_paise": fee_back, "gst_paise": gst_back,
                "processed_at": r["processed_at"],
            })
            refund_gross += amount
            refund_fee += fee_back
            refund_gst += gst_back

    def rupees(paise: int) -> Decimal:
        return _q(Decimal(paise) / 100)

    return {
        "period_start": first,
        "period_end": last,
        "online_gross": rupees(gross),
        "gross_collected": rupees(gross),
        "total_deduction": rupees(deduction),
        "bittu_fee": rupees(fee),
        "gst_on_fee": rupees(gst),
        "txn_count": txn_count,
        "unresolved_count": unresolved,
        "refund_count": len(refund_lines),
        "refund_gross": rupees(refund_gross),
        "refund_credit_fee": rupees(refund_fee),
        "refund_credit_gst": rupees(refund_gst),
        "refund_lines": refund_lines,
        "basis": "full_consideration",
    }


async def _persist_saas_invoice(
    *, merchant_id: str, year: int, month: int, currency: str, agg: dict,
) -> dict:
    """Upsert the month's invoice. Draft months are recomputed on every
    render; a month is frozen (issued) once it has ended and every payment is
    resolved. Frozen months are never rewritten — late refunds become credit
    notes. Returns the row plus its credit notes."""
    invoice_number = f"BITTU-{int(year):04d}{int(month):02d}-{str(merchant_id)[:8].upper()}"
    month_closed = _ist_today() > agg["period_end"]
    can_freeze = month_closed and agg["unresolved_count"] == 0
    subtotal = _q(agg["bittu_fee"] + agg["gst_on_fee"])
    net = _q(subtotal - agg["refund_credit_fee"] - agg["refund_credit_gst"])

    async with get_service_connection() as conn:
        existing = await conn.fetchrow(
            "SELECT * FROM bittu_saas_invoices WHERE merchant_id=$1::uuid AND period_year=$2 AND period_month=$3",
            str(merchant_id), int(year), int(month),
        )
        frozen = existing is not None and existing["frozen_at"] is not None
        if not frozen:
            status = "issued" if can_freeze else "draft"
            row = await conn.fetchrow(
                """
                INSERT INTO bittu_saas_invoices (
                  invoice_number, merchant_id, period_year, period_month, currency,
                  gross_collected, online_gross, total_deduction,
                  bittu_fee_amount, gst_on_fee_amount, total_payable,
                  refund_credit_fee, refund_credit_gst, refund_count,
                  net_payable, txn_count, unresolved_count, status, basis, source,
                  frozen_at, issued_at
                ) VALUES ($1, $2::uuid, $3, $4, $5, $6, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
                          'full_consideration', 'razorpay_route_mirror',
                          CASE WHEN $17 = 'issued' THEN NOW() END, NOW())
                ON CONFLICT (merchant_id, period_year, period_month) DO UPDATE
                  SET gross_collected   = EXCLUDED.gross_collected,
                      online_gross      = EXCLUDED.online_gross,
                      total_deduction   = EXCLUDED.total_deduction,
                      bittu_fee_amount  = EXCLUDED.bittu_fee_amount,
                      gst_on_fee_amount = EXCLUDED.gst_on_fee_amount,
                      total_payable     = EXCLUDED.total_payable,
                      refund_credit_fee = EXCLUDED.refund_credit_fee,
                      refund_credit_gst = EXCLUDED.refund_credit_gst,
                      refund_count      = EXCLUDED.refund_count,
                      net_payable       = EXCLUDED.net_payable,
                      txn_count         = EXCLUDED.txn_count,
                      unresolved_count  = EXCLUDED.unresolved_count,
                      status            = EXCLUDED.status,
                      basis             = EXCLUDED.basis,
                      source            = EXCLUDED.source,
                      frozen_at         = EXCLUDED.frozen_at,
                      issued_at         = CASE WHEN EXCLUDED.status = 'issued' THEN NOW() ELSE bittu_saas_invoices.issued_at END,
                      updated_at        = NOW()
                RETURNING *
                """,
                invoice_number, str(merchant_id), int(year), int(month), currency.upper(),
                agg["gross_collected"], agg["total_deduction"],
                agg["bittu_fee"], agg["gst_on_fee"], subtotal,
                agg["refund_credit_fee"], agg["refund_credit_gst"], int(agg["refund_count"]),
                net, int(agg["txn_count"]), int(agg["unresolved_count"]), status,
            )
        else:
            row = existing
            # Late refunds against a frozen month → credit notes (idempotent per refund).
            for line in agg["refund_lines"]:
                total = _q(Decimal(line["fee_paise"] + line["gst_paise"]) / 100)
                if total <= 0:
                    continue
                inserted = await conn.fetchval(
                    """
                    INSERT INTO bittu_saas_credit_notes (
                      credit_note_number, invoice_id, merchant_id, refund_id, razorpay_payment_id, currency,
                      recovered_amount, fee_credit, gst_credit, total_credit, reason, metadata
                    ) VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, 'refund_after_invoice_frozen', $11::jsonb)
                    ON CONFLICT (refund_id) DO NOTHING
                    RETURNING id
                    """,
                    f"{row['invoice_number']}-CN-{line['refund_id']}", int(row["id"]), str(merchant_id),
                    line["refund_id"], line["razorpay_payment_id"], currency.upper(),
                    _q(Decimal(line["recovered_paise"]) / 100), _q(Decimal(line["fee_paise"]) / 100),
                    _q(Decimal(line["gst_paise"]) / 100), total,
                    json.dumps({"processed_at": line["processed_at"].isoformat() if line["processed_at"] else None}),
                )
                if inserted is not None and row["status"] == "issued":
                    row = await conn.fetchrow(
                        "UPDATE bittu_saas_invoices SET status='credit_noted', updated_at=NOW() WHERE id=$1 RETURNING *",
                        int(row["id"]),
                    )
        notes = await conn.fetch(
            "SELECT * FROM bittu_saas_credit_notes WHERE invoice_id=$1 ORDER BY created_at, id", int(row["id"]),
        )
    result = dict(row)
    result["credit_notes"] = [dict(n) for n in notes]
    return result


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

    is_draft = inv["status"] == "draft"
    watermark = ""
    if is_draft:
        reason = (
            f"{int(inv['unresolved_count'])} payment(s) awaiting Route allocation"
            if int(inv["unresolved_count"] or 0) > 0 else "billing period still open"
        )
        watermark = (
            "<div style='border:2px dashed #b91c1c; color:#b91c1c; padding:6pt 10pt; margin-bottom:10pt; "
            f"font-weight:bold; text-align:center;'>DRAFT — not a tax invoice ({_esc(reason)})</div>"
        )

    credit_rows = "".join(
        "<tr>"
        f"<td>Credit note {_esc(n['credit_note_number'])} — refund {_esc(n['refund_id'])}</td>"
        f"<td class='num'>−{_money(n['fee_credit'])}</td>"
        f"<td class='num'>−{_money(n['gst_credit'])}</td>"
        f"<td class='num'>−{_money(n['total_credit'])}</td>"
        "</tr>"
        for n in inv.get("credit_notes") or []
    )
    credit_total = sum((Decimal(str(n["total_credit"])) for n in inv.get("credit_notes") or []), Decimal(0))

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
    <div class='muted'>{'Monthly SaaS / Payment-Processing Tax Invoice' if not is_draft else 'Monthly SaaS / Payment-Processing Statement (draft)'}</div>
  </div>
  {watermark}

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
        <strong>Status:</strong> {_esc(inv['status'])}<br/>
        <strong>Issued:</strong> {_esc((inv.get('frozen_at') or inv.get('issued_at')).date().isoformat() if (inv.get('frozen_at') or inv.get('issued_at')) else date.today().isoformat())}<br/>
        <strong>Currency:</strong> {_esc(currency)}
      </td>
    </tr>
  </table>

  <table>
    <thead>
      <tr><th>Description</th><th class='num' style='width:18%;'>Taxable</th><th class='num' style='width:18%;'>GST 18%</th><th class='num' style='width:18%;'>Total</th></tr>
    </thead>
    <tbody>
      <tr>
        <td>Online payments collected via Razorpay Route ({int(inv['txn_count'])} transactions) — informational</td>
        <td class='num'>{_money(inv['online_gross'])}</td><td class='num'>—</td><td class='num'>—</td>
      </tr>
      <tr>
        <td>Platform service fee (5% of online collections, GST-inclusive)</td>
        <td class='num'>{_money(inv['bittu_fee_amount'])}</td>
        <td class='num'>{_money(inv['gst_on_fee_amount'])}</td>
        <td class='num'>{_money(inv['total_payable'])}</td>
      </tr>
      {"<tr><td>Less: fee on refunds processed this period (" + str(int(inv['refund_count'])) + ")</td>"
       f"<td class='num'>−{_money(inv['refund_credit_fee'])}</td><td class='num'>−{_money(inv['refund_credit_gst'])}</td>"
       f"<td class='num'>−{_money(Decimal(str(inv['refund_credit_fee'])) + Decimal(str(inv['refund_credit_gst'])))}</td></tr>" if int(inv['refund_count'] or 0) else ""}
      {credit_rows}
    </tbody>
  </table>

  <table class='totals' style='margin-top: 10pt;'>
    <tr><td class='label'>Taxable value</td><td class='value'>{_money(Decimal(str(inv['bittu_fee_amount'])) - Decimal(str(inv['refund_credit_fee'])))}</td></tr>
    <tr><td class='label'>GST @ 18%</td><td class='value'>{_money(Decimal(str(inv['gst_on_fee_amount'])) - Decimal(str(inv['refund_credit_gst'])))}</td></tr>
    {"<tr><td class='label'>Credit notes</td><td class='value'>−" + _money(credit_total) + "</td></tr>" if credit_total else ""}
    <tr class='grand'><td class='label'>Net fee for the period</td>
        <td class='value'>{_money(Decimal(str(inv['net_payable'])) - credit_total)}</td></tr>
  </table>

  <p class='muted' style='margin-top: 24pt;'>
    Taxable value = the full 5% deduction on each online payment (merchant receives 95%), split into
    platform fee and 18% GST, rounded once per payment in paise. Gateway charges are Bittu's cost and are
    not billed to you. Fees were deducted via Razorpay Route at capture; no further payment is due.
  </p>
</body></html>
"""
    pdf_bytes = _html_to_pdf(html_doc)
    filename = f"bittu_saas_invoice_{int(year):04d}_{int(month):02d}{'_draft' if is_draft else ''}.pdf"
    return pdf_bytes, filename


# ────────────────────────────────────────────────────────────────────
# Purchase order (to vendor)
# ────────────────────────────────────────────────────────────────────
async def render_purchase_order(*, order: dict) -> tuple[bytes, str]:
    """Render a purchase order dict (from PurchaseOrderService.get_order)
    into a printable / e-mailable PDF. Returns (pdf_bytes, filename)."""
    items = order.get("items") or []

    rows_parts: list[str] = []
    for i, it in enumerate(items, start=1):
        qty = Decimal(str(it.get("quantity_ordered") or 0))
        price = Decimal(str(it.get("unit_price") or 0))
        amount = Decimal(str(it.get("amount") or (qty * price)))
        rows_parts.append(
            "<tr>"
            f"<td class='num'>{i}</td>"
            f"<td>{_esc(it.get('ingredient_name') or it.get('ingredient_id') or '')}</td>"
            f"<td class='num'>{qty.normalize()}</td>"
            f"<td>{_esc(it.get('unit') or '')}</td>"
            f"<td class='num'>{_money(price)}</td>"
            f"<td class='num'>{_money(amount)}</td>"
            "</tr>"
        )
    rows_html = "".join(rows_parts) or (
        "<tr><td colspan='6' class='center'>(no line items)</td></tr>"
    )

    supplier = (
        order.get("supplier_name") or order.get("source_name") or "Supplier"
    )
    po_number = order.get("po_number") or f"PO-{order.get('id')}"
    expected = order.get("expected_delivery_date")
    expected_str = expected.isoformat() if hasattr(expected, "isoformat") else (expected or "-")
    approval = order.get("approval_status") or "draft"

    html_doc = f"""
<html><head><meta charset='utf-8'/>
<style>
  body {{ font-family: Helvetica, Arial, sans-serif; font-size: 10pt; color: #222; }}
  h1 {{ font-size: 18pt; margin: 0; color: #1d4ed8; }}
  .muted {{ color: #777; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 12pt; }}
  th, td {{ border: 1px solid #ccc; padding: 5pt 8pt; text-align: left; }}
  th {{ background: #f1f5f9; }}
  .num {{ text-align: right; }}
  .center {{ text-align: center; }}
  .totals td {{ border: none; padding: 3pt 8pt; }}
  .totals .label {{ text-align: right; color: #555; }}
  .totals .value {{ text-align: right; font-weight: bold; width: 30%; }}
  .grand {{ border-top: 2px solid #1d4ed8 !important; font-size: 13pt; color: #1d4ed8; }}
</style></head>
<body>
  <div>
    <h1>Purchase Order</h1>
    <div class='muted'>Status: {_esc(approval)}</div>
  </div>

  <table style='border:none; margin-top:0;'>
    <tr style='border:none;'>
      <td style='border:none; width:60%;'>
        <strong>Supplier:</strong><br/>
        {_esc(supplier)}<br/>
        {_esc(order.get('supplier_contact') or '')}
      </td>
      <td style='border:none; text-align:right;'>
        <strong>PO #:</strong> {_esc(po_number)}<br/>
        <strong>Expected delivery:</strong> {_esc(expected_str)}<br/>
        <strong>Payment:</strong> {_esc(order.get('payment_status') or 'unpaid')}
      </td>
    </tr>
  </table>

  <table>
    <thead>
      <tr>
        <th style='width:5%;'>#</th>
        <th>Item</th>
        <th class='num' style='width:12%;'>Qty</th>
        <th style='width:12%;'>Unit</th>
        <th class='num' style='width:15%;'>Unit Price</th>
        <th class='num' style='width:18%;'>Amount</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>

  <table class='totals' style='margin-top: 10pt;'>
    <tr><td class='label'>Subtotal</td><td class='value'>{_money(order.get('sub_total'))}</td></tr>
    <tr><td class='label'>Delivery</td><td class='value'>{_money(order.get('delivery_charges'))}</td></tr>
    <tr class='grand'><td class='label'>Total</td><td class='value'>{_money(order.get('total_amount'))}</td></tr>
  </table>

  <p class='muted' style='margin-top: 20pt;'>{_esc(order.get('notes') or '')}</p>
  <p class='muted' style='margin-top: 24pt; text-align: center;'>
    This purchase order was generated by Bittu.
  </p>
</body></html>
"""
    pdf_bytes = _html_to_pdf(html_doc)
    filename = f"purchase_order_{po_number}.pdf"
    return pdf_bytes, filename

