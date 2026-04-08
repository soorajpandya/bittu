"""PDF generation service using WeasyPrint + Jinja2.

Generates PDF documents for accounting entities:
invoices, estimates, sales orders, purchase orders, credit notes,
debit notes, bills, retainer invoices, vendor credits, sales receipts.
"""
import io
import json
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

from jinja2 import Environment, FileSystemLoader
from xhtml2pdf import pisa

from app.core.auth import UserContext
from app.core.database import get_connection

# ── Template setup ────────────────────────────────────────────
TEMPLATE_DIR = Path(__file__).parent.parent / "templates" / "pdf"
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=True,
)


def _fmt_currency(value: Any, symbol: str = "₹", precision: int = 2) -> str:
    try:
        return f"{symbol}{float(value):,.{precision}f}"
    except (TypeError, ValueError):
        return f"{symbol}0.00"


def _fmt_date(value: Any, fmt: str = "%d %b %Y") -> str:
    if not value:
        return ""
    if isinstance(value, str):
        for pattern in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return datetime.strptime(value, pattern).strftime(fmt)
            except ValueError:
                continue
        return value
    if isinstance(value, (datetime, date)):
        return value.strftime(fmt)
    return str(value)


_env.filters["currency"] = _fmt_currency
_env.filters["fdate"] = _fmt_date


# ── Org info helper ───────────────────────────────────────────
async def _get_org(user: UserContext) -> dict:
    uid = user.owner_id if user.is_branch_user else user.user_id
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM acc_organizations WHERE user_id = $1 AND is_default_org = true LIMIT 1",
            uid,
        )
        if not row:
            row = await conn.fetchrow(
                "SELECT * FROM acc_organizations WHERE user_id = $1 LIMIT 1",
                uid,
            )

        org = dict(row) if row else {}

        # Fetch restaurant details to fill in missing org info
        rest = await conn.fetchrow(
            """SELECT r.name, r.phone, r.email, r.address, r.city, r.state,
                      r.pincode, r.logo_url, r.gst_number, r.fssai_number
               FROM restaurants r WHERE r.owner_id = $1 LIMIT 1""",
            uid,
        )
        rest = dict(rest) if rest else {}

        # Use restaurant name if org name is missing or default
        org_name = org.get("name") or ""
        if not org_name or org_name == "My Organization":
            org["name"] = rest.get("name") or org_name or "My Organization"

        # Fill other fields from restaurant if org doesn't have them
        if not org.get("phone") and rest.get("phone"):
            org["phone"] = rest["phone"]
        if not org.get("email") and rest.get("email"):
            org["email"] = rest["email"]
        if not org.get("logo_url") and rest.get("logo_url"):
            org["logo_url"] = rest["logo_url"]

        # Build address from restaurant if org address is empty
        org_addr = org.get("address")
        if not org_addr or org_addr == {} or org_addr == "{}":
            org["address"] = {
                "street": rest.get("address") or "",
                "city": rest.get("city") or "",
                "state": rest.get("state") or "",
                "zip": rest.get("pincode") or "",
            }

        # GST info
        if not org.get("tax_id_value") and rest.get("gst_number"):
            org["tax_id_label"] = "GSTIN"
            org["tax_id_value"] = rest["gst_number"]

        # FSSAI
        if rest.get("fssai_number"):
            org["fssai_number"] = rest["fssai_number"]

        # Defaults
        org.setdefault("currency_symbol", "₹")
        org.setdefault("price_precision", 2)
        org.setdefault("address", {})

        return org


async def _get_contact(contact_id: UUID, user: UserContext) -> dict:
    uid = user.owner_id if user.is_branch_user else user.user_id
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM acc_contacts WHERE contact_id = $1 AND user_id = $2",
            contact_id, uid,
        )
        return dict(row) if row else {}


async def _get_line_items(parent_table: str, parent_pk: str, parent_id: UUID, line_type: str, user: UserContext) -> list[dict]:
    uid = user.owner_id if user.is_branch_user else user.user_id
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"""SELECT li.* FROM acc_line_items li
                JOIN {parent_table} p ON p.{parent_pk} = li.parent_id
                WHERE li.parent_id = $1 AND li.parent_type = $2 AND p.user_id = $3
                ORDER BY li.item_order, li.created_at""",
            parent_id, line_type, uid,
        )
        return [dict(r) for r in rows]


# ── Core renderer ─────────────────────────────────────────────
def _render_pdf(template_name: str, context: dict) -> bytes:
    template = _env.get_template(template_name)
    html_string = template.render(**context)
    buf = io.BytesIO()
    pisa_status = pisa.CreatePDF(io.StringIO(html_string), dest=buf)
    if pisa_status.err:
        raise RuntimeError("PDF generation failed")
    buf.seek(0)
    return buf.read()


# ── Document config ───────────────────────────────────────────
DOC_CONFIG = {
    "invoice": {
        "table": "acc_invoices",
        "pk": "invoice_id",
        "contact_field": "customer_id",
        "number_field": "invoice_number",
        "label": "Invoice",
        "template": "invoice.html",
        "line_type": "invoice",
    },
    "estimate": {
        "table": "acc_estimates",
        "pk": "estimate_id",
        "contact_field": "customer_id",
        "number_field": "estimate_number",
        "label": "Estimate",
        "template": "estimate.html",
        "line_type": "estimate",
    },
    "salesorder": {
        "table": "acc_sales_orders",
        "pk": "salesorder_id",
        "contact_field": "customer_id",
        "number_field": "salesorder_number",
        "label": "Sales Order",
        "template": "salesorder.html",
        "line_type": "sales_order",
    },
    "purchaseorder": {
        "table": "acc_purchase_orders",
        "pk": "purchaseorder_id",
        "contact_field": "vendor_id",
        "number_field": "purchaseorder_number",
        "label": "Purchase Order",
        "template": "purchaseorder.html",
        "line_type": "purchase_order",
    },
    "creditnote": {
        "table": "acc_credit_notes",
        "pk": "creditnote_id",
        "contact_field": "customer_id",
        "number_field": "creditnote_number",
        "label": "Credit Note",
        "template": "creditnote.html",
        "line_type": "credit_note",
    },
    "debitnote": {
        "table": "acc_debit_notes",
        "pk": "debitnote_id",
        "contact_field": "vendor_id",
        "number_field": "debitnote_number",
        "label": "Debit Note",
        "template": "debitnote.html",
        "line_type": "debit_note",
    },
    "bill": {
        "table": "acc_bills",
        "pk": "bill_id",
        "contact_field": "vendor_id",
        "number_field": "bill_number",
        "label": "Bill",
        "template": "bill.html",
        "line_type": "bill",
    },
    "retainerinvoice": {
        "table": "acc_retainer_invoices",
        "pk": "retainerinvoice_id",
        "contact_field": "customer_id",
        "number_field": "retainerinvoice_number",
        "label": "Retainer Invoice",
        "template": "retainerinvoice.html",
        "line_type": "retainer_invoice",
    },
    "vendorcredit": {
        "table": "acc_vendor_credits",
        "pk": "vendorcredit_id",
        "contact_field": "vendor_id",
        "number_field": "vendorcredit_number",
        "label": "Vendor Credit",
        "template": "vendorcredit.html",
        "line_type": "vendor_credit",
    },
    "salesreceipt": {
        "table": "acc_sales_receipts",
        "pk": "salesreceipt_id",
        "contact_field": "customer_id",
        "number_field": "salesreceipt_number",
        "label": "Sales Receipt",
        "template": "salesreceipt.html",
        "line_type": "sales_receipt",
    },
}


# ── Public API ────────────────────────────────────────────────
async def generate_document_pdf(
    doc_type: str,
    doc_id: UUID,
    user: UserContext,
) -> tuple[bytes, str]:
    """Generate PDF for a single document. Returns (pdf_bytes, filename)."""
    cfg = DOC_CONFIG.get(doc_type)
    if not cfg:
        raise ValueError(f"Unsupported document type: {doc_type}")

    uid = user.owner_id if user.is_branch_user else user.user_id

    async with get_connection() as conn:
        doc = await conn.fetchrow(
            f"SELECT * FROM {cfg['table']} WHERE {cfg['pk']} = $1 AND user_id = $2",
            doc_id, uid,
        )

    if not doc:
        raise ValueError(f"{cfg['label']} not found")

    doc = dict(doc)
    org = await _get_org(user)

    # Fetch related contact
    contact = {}
    contact_id = doc.get(cfg["contact_field"])
    if contact_id:
        contact = await _get_contact(contact_id, user)

    # Fetch line items
    line_items = await _get_line_items(cfg["table"], cfg["pk"], doc_id, cfg["line_type"], user)

    # Parse JSONB fields
    for field in ("billing_address", "shipping_address", "custom_fields", "tags"):
        val = doc.get(field)
        if isinstance(val, str):
            try:
                doc[field] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass

    org_address = org.get("address", {})
    if isinstance(org_address, str):
        try:
            org_address = json.loads(org_address)
        except (json.JSONDecodeError, TypeError):
            org_address = {}

    context = {
        "doc": doc,
        "org": org,
        "org_address": org_address,
        "contact": contact,
        "line_items": line_items,
        "currency_symbol": org.get("currency_symbol", "₹"),
        "precision": org.get("price_precision", 2),
        "generated_at": datetime.utcnow(),
    }

    # Use specific template if exists, fallback to generic
    template_name = cfg["template"]
    try:
        _env.get_template(template_name)
    except Exception:
        template_name = "generic_document.html"

    pdf_bytes = _render_pdf(template_name, context)
    doc_number = doc.get(cfg["number_field"], str(doc_id)[:8])
    filename = f"{cfg['label'].replace(' ', '_')}_{doc_number}.pdf"

    return pdf_bytes, filename


async def generate_bulk_pdf(
    doc_type: str,
    doc_ids: list[UUID],
    user: UserContext,
) -> tuple[bytes, str]:
    """Generate a ZIP file containing PDFs for multiple documents."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for doc_id in doc_ids:
            try:
                pdf_bytes, filename = await generate_document_pdf(doc_type, doc_id, user)
                zf.writestr(filename, pdf_bytes)
            except ValueError:
                continue

    buf.seek(0)
    cfg = DOC_CONFIG.get(doc_type, {})
    label = cfg.get("label", doc_type).replace(" ", "_")
    zip_filename = f"{label}_Export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.zip"
    return buf.read(), zip_filename


async def generate_print_html(
    doc_type: str,
    doc_id: UUID,
    user: UserContext,
) -> str:
    """Generate printable HTML (same template, returned as string for browser print)."""
    cfg = DOC_CONFIG.get(doc_type)
    if not cfg:
        raise ValueError(f"Unsupported document type: {doc_type}")

    uid = user.owner_id if user.is_branch_user else user.user_id

    async with get_connection() as conn:
        doc = await conn.fetchrow(
            f"SELECT * FROM {cfg['table']} WHERE {cfg['pk']} = $1 AND user_id = $2",
            doc_id, uid,
        )

    if not doc:
        raise ValueError(f"{cfg['label']} not found")

    doc = dict(doc)
    org = await _get_org(user)
    contact = {}
    contact_id = doc.get(cfg["contact_field"])
    if contact_id:
        contact = await _get_contact(contact_id, user)

    line_items = await _get_line_items(cfg["table"], cfg["pk"], doc_id, user)

    for field in ("billing_address", "shipping_address", "custom_fields", "tags"):
        val = doc.get(field)
        if isinstance(val, str):
            try:
                doc[field] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass

    org_address = org.get("address", {})
    if isinstance(org_address, str):
        try:
            org_address = json.loads(org_address)
        except (json.JSONDecodeError, TypeError):
            org_address = {}

    context = {
        "doc": doc,
        "org": org,
        "org_address": org_address,
        "contact": contact,
        "line_items": line_items,
        "currency_symbol": org.get("currency_symbol", "₹"),
        "precision": org.get("price_precision", 2),
        "generated_at": datetime.utcnow(),
    }

    template_name = cfg["template"]
    try:
        _env.get_template(template_name)
    except Exception:
        template_name = "generic_document.html"

    template = _env.get_template(template_name)
    return template.render(**context)
