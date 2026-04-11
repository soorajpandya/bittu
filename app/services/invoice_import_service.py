"""
AI-Powered Sales Invoice Import Service.

Flow:
  1. Accept invoice image (base64) or PDF
  2. OCR extraction (Google Vision or fallback)
  3. OpenAI GPT-4o structuring → strict JSON
  4. Match items to existing ingredients (fuzzy)
  5. Return parsed preview (do NOT auto-save)
  6. On confirmation: create purchase_invoice, update inventory, record expense
"""
import json
import hashlib
import base64
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

import httpx

from app.core.config import get_settings
from app.core.database import get_connection, get_serializable_transaction
from app.core.tenant import tenant_where_clause, tenant_insert_fields
from app.core.exceptions import ValidationError, ConflictError, NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
GOOGLE_VISION_URL = "https://vision.googleapis.com/v1/images:annotate"

INVOICE_SYSTEM_PROMPT = """You are an expert Indian accounting assistant specializing in parsing purchase invoices and sales bills.

You will receive an image of a Sales Invoice / Bill from a vendor (supplier). Your task is to extract ALL structured data from it.

CRITICAL RULES:
- Extract EVERY line item. Miss NOTHING.
- Handle messy, handwritten, blurry, or partially printed Indian bills with high tolerance.
- Convert all prices to numbers (no currency symbols).
- Dates should be in YYYY-MM-DD format. If only DD-MM-YY or DD/MM/YYYY, convert accordingly.
- If GST details are visible (GSTIN, tax rate, CGST/SGST/IGST), extract them.
- If a field is not visible or missing, use null.
- Return ONLY valid JSON. No markdown, no explanation, no commentary.

OUTPUT FORMAT:
{
  "vendor_name": "string or null",
  "vendor_gstin": "string or null",
  "invoice_number": "string or null",
  "invoice_date": "YYYY-MM-DD or null",
  "items": [
    {
      "name": "string",
      "hsn_code": "string or null",
      "quantity": number,
      "unit": "string (kg/g/L/ml/pcs/Nos/etc.)",
      "unit_price": number,
      "discount_percent": number or 0,
      "tax_percent": number or 0,
      "tax_amount": number or 0,
      "line_total": number
    }
  ],
  "subtotal": number or null,
  "tax_amount": number or null,
  "total_amount": number,
  "payment_status": "paid" or "unpaid" or "partial",
  "notes": "any additional text/remarks from the bill or null"
}"""


def _cfg():
    return get_settings()


class InvoiceImportService:

    # ── STEP 1: Parse invoice image ──

    async def parse_invoice(
        self,
        image_base64: str,
        mime_type: str = "image/jpeg",
        idempotency_key: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        """
        Parse an invoice image using OCR + OpenAI.
        Returns structured data for frontend preview — does NOT save anything.
        """
        s = _cfg()
        if not s.OPENAI_API_KEY:
            raise ValidationError("OPENAI_API_KEY is not configured")

        # Generate idempotency key from image hash if not provided
        if not idempotency_key:
            img_bytes = base64.b64decode(image_base64[:1000])  # hash first chunk
            idempotency_key = hashlib.sha256(img_bytes).hexdigest()[:32]

        # Check for duplicate import
        if user_id:
            async with get_connection() as conn:
                existing = await conn.fetchrow(
                    "SELECT id, status FROM purchase_invoices WHERE idempotency_key = $1 AND user_id = $2",
                    idempotency_key, user_id,
                )
                if existing:
                    raise ConflictError(
                        f"This invoice was already imported (ID: {existing['id']}, status: {existing['status']})"
                    )

        # OCR (optional enrichment)
        ocr_text = None
        if s.GOOGLE_VISION_API_KEY:
            try:
                ocr_text = await self._google_ocr(image_base64)
            except Exception:
                logger.warning("invoice_google_ocr_failed")

        # GPT-4o Vision parse
        parsed = await self._openai_parse(image_base64, mime_type, ocr_text)

        # Match items to existing ingredients
        if user_id:
            parsed["items"] = await self._match_ingredients(user_id, parsed.get("items", []))

        parsed["idempotency_key"] = idempotency_key
        parsed["ocr_text"] = ocr_text or ""

        logger.info("invoice_parsed", items=len(parsed.get("items", [])), vendor=parsed.get("vendor_name"))
        return parsed

    # ── STEP 2: Confirm and save parsed invoice ──

    async def confirm_invoice(
        self,
        user_id: str,
        restaurant_id: Optional[str],
        branch_id: Optional[str],
        parsed_data: dict,
        purchase_order_id: Optional[str] = None,
    ) -> dict:
        """
        Save the parsed (and potentially user-edited) invoice data.
        Creates:
        - purchase_invoices record
        - purchase_invoice_items records
        - Ingredient records for new items
        - Inventory transactions (stock increase)
        - Accounting expense entry
        """
        idempotency_key = parsed_data.get("idempotency_key")

        async with get_serializable_transaction() as conn:
            # Double-check idempotency
            if idempotency_key:
                dup = await conn.fetchrow(
                    "SELECT id FROM purchase_invoices WHERE idempotency_key = $1",
                    idempotency_key,
                )
                if dup:
                    raise ConflictError(f"Invoice already saved (ID: {dup['id']})")

            # Parse date
            inv_date = None
            if parsed_data.get("invoice_date"):
                try:
                    inv_date = date.fromisoformat(parsed_data["invoice_date"])
                except (ValueError, TypeError):
                    inv_date = None

            total_amount = float(parsed_data.get("total_amount", 0))
            subtotal = float(parsed_data.get("subtotal") or total_amount)
            tax_amount = float(parsed_data.get("tax_amount") or 0)

            # Create purchase invoice header
            inv_row = await conn.fetchrow(
                """
                INSERT INTO purchase_invoices (
                    user_id, restaurant_id, branch_id, vendor_name, vendor_gstin,
                    invoice_number, invoice_date, subtotal, tax_amount, total_amount,
                    payment_status, status, purchase_order_id, raw_ocr_text,
                    raw_ai_response, idempotency_key, notes
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'confirmed',$12,$13,$14,$15,$16)
                RETURNING id, created_at
                """,
                user_id, restaurant_id, branch_id,
                parsed_data.get("vendor_name"),
                parsed_data.get("vendor_gstin"),
                parsed_data.get("invoice_number"),
                inv_date,
                subtotal, tax_amount, total_amount,
                parsed_data.get("payment_status", "unpaid"),
                purchase_order_id,
                parsed_data.get("ocr_text"),
                json.dumps(parsed_data),
                idempotency_key,
                parsed_data.get("notes"),
            )
            invoice_id = str(inv_row["id"])

            # Process line items
            items = parsed_data.get("items", [])
            saved_items = []

            for item in items:
                ingredient_id = item.get("ingredient_id")
                item_name = item.get("name", "Unknown")
                quantity = float(item.get("quantity", 0))
                unit = item.get("unit", "pcs")
                unit_price = float(item.get("unit_price", 0))
                discount_pct = float(item.get("discount_percent", 0))
                tax_pct = float(item.get("tax_percent", 0))
                tax_amt = float(item.get("tax_amount", 0))
                line_total = float(item.get("line_total", 0))

                # Auto-create ingredient if not matched
                if not ingredient_id:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO ingredients (user_id, restaurant_id, name, unit, current_stock, stock_quantity, minimum_stock, cost_per_unit)
                        VALUES ($1, $2, $3, $4, 0, 0, 0, $5)
                        RETURNING id
                        """,
                        user_id, restaurant_id, item_name, unit,
                        unit_price,
                    )
                    ingredient_id = str(row["id"])

                # Insert invoice line item
                await conn.execute(
                    """
                    INSERT INTO purchase_invoice_items (
                        invoice_id, ingredient_id, item_name, hsn_code, quantity,
                        unit, unit_price, discount_percent, tax_percent, tax_amount, line_total
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                    """,
                    inv_row["id"], ingredient_id, item_name,
                    item.get("hsn_code"), quantity, unit, unit_price,
                    discount_pct, tax_pct, tax_amt, line_total,
                )

                # Update ingredient stock
                if quantity > 0:
                    await conn.execute(
                        """
                        UPDATE ingredients
                        SET current_stock = current_stock + $1,
                            stock_quantity = stock_quantity + $1,
                            cost_per_unit = CASE WHEN $2 > 0 THEN $2 ELSE cost_per_unit END,
                            updated_at = now()
                        WHERE id = $3
                        """,
                        quantity, unit_price, ingredient_id,
                    )

                    # Log inventory transaction
                    await conn.execute(
                        """
                        INSERT INTO inventory_transactions
                            (restaurant_id, ingredient_id, type, quantity, reference_id, performed_by, notes)
                        VALUES ($1, $2, 'purchase'::inventory_txn_type, $3, $4, $5, $6)
                        """,
                        restaurant_id, ingredient_id, quantity,
                        invoice_id, user_id,
                        f"Invoice #{parsed_data.get('invoice_number', 'N/A')} from {parsed_data.get('vendor_name', 'Unknown')}",
                    )

                saved_items.append({
                    "ingredient_id": ingredient_id,
                    "name": item_name,
                    "quantity": quantity,
                    "unit": unit,
                    "unit_price": unit_price,
                    "line_total": line_total,
                })

            # Record expense in accounting
            if total_amount > 0:
                await conn.execute(
                    """
                    INSERT INTO accounting_entries
                        (user_id, restaurant_id, branch_id, entry_type, amount,
                         category, reference_type, reference_id, description)
                    VALUES ($1, $2, $3, 'expense', $4, 'purchase',
                            'purchase_invoice', $5, $6)
                    """,
                    user_id, restaurant_id, branch_id,
                    -abs(total_amount),
                    invoice_id,
                    f"Invoice #{parsed_data.get('invoice_number', 'N/A')} from {parsed_data.get('vendor_name', 'Unknown')}",
                )

            # Record cash transaction
            await conn.execute(
                """
                INSERT INTO cash_transactions (user_id, branch_id, type, amount, description, category, payment_method)
                VALUES ($1, $2, 'expense', $3, $4, 'purchase', 'cash')
                """,
                user_id, branch_id, total_amount,
                f"Invoice #{parsed_data.get('invoice_number', 'N/A')} from {parsed_data.get('vendor_name', 'Unknown')}",
            )

        logger.info("invoice_confirmed", invoice_id=invoice_id, items=len(saved_items), total=total_amount)
        return {
            "invoice_id": invoice_id,
            "vendor_name": parsed_data.get("vendor_name"),
            "invoice_number": parsed_data.get("invoice_number"),
            "total_amount": total_amount,
            "items_count": len(saved_items),
            "items": saved_items,
            "status": "confirmed",
        }

    # ── Get invoice by ID ──

    async def get_invoice(self, user_id: str, invoice_id: str) -> dict:
        async with get_connection() as conn:
            inv = await conn.fetchrow(
                "SELECT * FROM purchase_invoices WHERE id = $1 AND user_id = $2",
                invoice_id, user_id,
            )
            if not inv:
                raise NotFoundError("PurchaseInvoice", invoice_id)

            items = await conn.fetch(
                "SELECT * FROM purchase_invoice_items WHERE invoice_id = $1 ORDER BY created_at",
                inv["id"],
            )
            result = dict(inv)
            result["items"] = [dict(i) for i in items]
            return result

    # ── List invoices ──

    async def list_invoices(
        self,
        user_id: str,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        async with get_connection() as conn:
            params = [user_id]
            sql = "SELECT * FROM purchase_invoices WHERE user_id = $1"
            if status:
                params.append(status)
                sql += f" AND status = ${len(params)}"
            params.extend([limit, offset])
            sql += f" ORDER BY created_at DESC LIMIT ${len(params)-1} OFFSET ${len(params)}"
            rows = await conn.fetch(sql, *params)
            return [dict(r) for r in rows]

    # ── Internal: OpenAI Vision parse ──

    async def _openai_parse(
        self,
        image_base64: str,
        mime_type: str,
        ocr_text: Optional[str] = None,
    ) -> dict:
        s = _cfg()

        user_content = []
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
        })

        if ocr_text:
            user_content.append({
                "type": "text",
                "text": f"OCR text extracted from this invoice (use as reference):\n{ocr_text}",
            })
        else:
            user_content.append({
                "type": "text",
                "text": "Parse this sales invoice / bill image and extract all structured data.",
            })

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                OPENAI_CHAT_URL,
                headers={
                    "Authorization": f"Bearer {s.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "system", "content": INVOICE_SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 4000,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        content = data["choices"][0]["message"]["content"].strip()

        # Strip markdown fences
        if content.startswith("```"):
            content = content.split("\n", 1)[1]
            content = content.rsplit("```", 1)[0]

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            logger.error("invoice_ai_json_parse_failed", content=content[:500])
            raise ValidationError("AI failed to produce valid JSON. Please try again with a clearer image.")

        return parsed

    # ── Internal: Google Vision OCR ──

    async def _google_ocr(self, image_base64: str) -> Optional[str]:
        s = _cfg()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{GOOGLE_VISION_URL}?key={s.GOOGLE_VISION_API_KEY}",
                json={
                    "requests": [{
                        "image": {"content": image_base64},
                        "features": [{"type": "TEXT_DETECTION", "maxResults": 1}],
                    }]
                },
            )
            resp.raise_for_status()
            data = resp.json()

        annotations = data.get("responses", [{}])[0].get("textAnnotations", [])
        if annotations:
            return annotations[0].get("description", "")
        return None

    # ── Internal: Match items to existing ingredients ──

    async def _match_ingredients(self, user_id: str, items: list[dict]) -> list[dict]:
        """Try to match parsed item names with existing ingredients."""
        if not items:
            return items

        async with get_connection() as conn:
            existing = await conn.fetch(
                "SELECT id, name, unit FROM ingredients WHERE user_id = $1",
                user_id,
            )
            # Build lookup (lowercase name → ingredient)
            lookup = {r["name"].lower().strip(): dict(r) for r in existing}

            for item in items:
                name = (item.get("name") or "").lower().strip()
                if name in lookup:
                    item["ingredient_id"] = lookup[name]["id"]
                    item["match_status"] = "exact"
                else:
                    # Try partial match
                    matched = False
                    for existing_name, ing in lookup.items():
                        if name in existing_name or existing_name in name:
                            item["ingredient_id"] = ing["id"]
                            item["match_status"] = "partial"
                            matched = True
                            break
                    if not matched:
                        item["ingredient_id"] = None
                        item["match_status"] = "new"

        return items
