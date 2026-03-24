"""
AI Menu Scanner — OpenAI GPT-4o Vision + Google Cloud Vision OCR.

Flow:
  1. Client uploads menu image (base64)
  2. (Optional) Google Vision OCR extracts raw text
  3. GPT-4o Vision parses image into structured menu items
  4. Returns JSON array of menu items
"""
import base64
import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
GOOGLE_VISION_URL = "https://vision.googleapis.com/v1/images:annotate"

SYSTEM_PROMPT = """You are an expert restaurant menu parser. You will receive a menu IMAGE. Your task is to extract EVERY SINGLE item visible on the menu — miss NOTHING.

MENU STRUCTURE:
- Bold/highlighted text or headings are CATEGORIES (e.g. "ભાજી પાઉ", "पाव भाजी", "Starters", "Beverages"). Do NOT output category headings as items.
- Lines of text below each heading are ITEMS belonging to that category.
- Columns on the right side are VARIANTS (e.g. "OIL" / "BUTTER", "Half" / "Full", "Small" / "Medium" / "Large"). Each column header is a variant name.
- If an item has MULTIPLE price columns (variants), create a SEPARATE entry for each variant with the variant in parentheses, e.g. "Bhaji Pav (Oil)" ₹125 and "Bhaji Pav (Butter)" ₹145.
- If an item has only ONE price, output it as a single entry without variant suffix.
- If a price cell is empty/blank for a variant, SKIP that variant (do not output price 0).

LANGUAGE — CRITICAL:
- ALL item_name values MUST be in English (transliterated). NEVER output Gujarati, Hindi, or any non-Latin script.
- Transliterate naturally: "ભાજી પાઉ" → "Bhaji Pav", "મસાલા છાશ" → "Masala Chaas", "મીનરલ વોટર" → "Mineral Water", "લસણ ચટણી" → "Lasun Chutney", "ફ્રાય પાપડ" → "Fry Papad", "એકસ્ટ્રા પાઉ" → "Extra Pav", "ખાલી ભાજી" → "Khali Bhaji", "બોઈલ ભાજી પાઉ" → "Boil Bhaji Pav", "વેજ. પુલાવ" → "Veg Pulav", "ચીઝ પુલાવ" → "Cheese Pulav", "પાપડ રોસ્ટેડ" → "Papad Roasted", "મસાલા પાપડ" → "Masala Papad".
- ALL category values MUST also be in English.

COMPLETENESS — CRITICAL:
- You MUST extract every single item that has a price on the menu. Count carefully.
- Include EVERYTHING: main dishes, snacks, beverages, water, buttermilk, chutneys, papad, extras, add-ons, sides.
- After extracting, mentally re-read the menu top to bottom and verify you haven't missed any item. If you find a missing item, add it.
- Common items that get missed: extras (Extra Pav), chutneys (Lasun Chutney), beverages (Mineral Water, Masala Chaas), sides (Papad Roasted, Fry Papad, Masala Papad).

DEDUPLICATION:
- Each unique item at a given price point appears ONLY ONCE.
- If the same item appears in multiple languages on the menu, output it once in English.

PRICES:
- Numbers only (no currency symbols). Convert Gujarati/Hindi numerals (૧=1, ૨=2, ૩=3, ૪=4, ૫=5, ૬=6, ૭=7, ૮=8, ૯=9, ૦=0).

OTHER:
- Determine Veg/Non-Veg from name/context. Default Veg.
- Estimate spice level: "Mild"/"Medium"/"Hot"/"Extra Hot". Default "Medium".
- Estimate prep time in minutes (5-30).
- Generate short code from initials (2-4 chars uppercase), e.g. "Paneer Butter Masala" → "PBM".

Return ONLY a valid JSON array. No markdown, no explanation. Each object:
{
  "item_name": "string (English only, variant in parentheses if applicable)",
  "price": number,
  "category": "string (English)",
  "subcategory": "string or null",
  "cuisine": "string",
  "is_veg": true/false,
  "spice_level": "Mild"|"Medium"|"Hot"|"Extra Hot",
  "prep_time_min": number,
  "short_code": "string",
  "description": "short 1-line description or null"
}"""


def _cfg():
    return get_settings()


class MenuScannerService:

    async def scan_menu_image(self, image_base64: str, mime_type: str = "image/jpeg") -> dict:
        """
        Parse a menu image using GPT-4o Vision.
        Optionally enriches with Google Vision OCR if API key is configured.
        """
        s = _cfg()

        if not s.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is not configured on the server")

        ocr_text = None

        # Optional: Google Vision OCR (non-blocking supplement)
        if s.GOOGLE_VISION_API_KEY:
            try:
                ocr_text = await self._google_ocr(image_base64)
            except Exception:
                logger.warning("google_vision_ocr_failed")

        # GPT-4o Vision parsing
        items = await self._openai_vision_parse(image_base64, mime_type, ocr_text)

        logger.info("menu_scan_complete", items_found=len(items) if isinstance(items, list) else 0)
        return {
            "ocr_text": ocr_text or "",
            "menu": items,
            "item_count": len(items) if isinstance(items, list) else 0,
        }

    async def _openai_vision_parse(
        self, image_base64: str, mime_type: str, ocr_text: str | None
    ) -> list[dict]:
        """Call GPT-4o Vision to extract menu items."""
        s = _cfg()
        user_content: list[dict] = [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{image_base64}",
                    "detail": "high",
                },
            }
        ]
        if ocr_text:
            user_content.append({
                "type": "text",
                "text": f"Supplementary OCR text (use to cross-check, but trust what you SEE in the image):\n\n{ocr_text}",
            })

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                OPENAI_CHAT_URL,
                json={
                    "model": "gpt-4o",
                    "temperature": 0.05,
                    "max_tokens": 16384,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                },
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {s.OPENAI_API_KEY}",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        raw = data["choices"][0]["message"]["content"].strip()
        # Strip markdown fences if model wraps them
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        import json
        return json.loads(raw)

    async def _google_ocr(self, image_base64: str) -> str | None:
        """Extract text via Google Cloud Vision Document Text Detection."""
        s = _cfg()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{GOOGLE_VISION_URL}?key={s.GOOGLE_VISION_API_KEY}",
                json={
                    "requests": [
                        {
                            "image": {"content": image_base64},
                            "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                            "imageContext": {"languageHints": ["en", "hi", "gu"]},
                        }
                    ]
                },
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        annotations = data.get("responses", [{}])[0].get("fullTextAnnotation", {})
        return annotations.get("text")
