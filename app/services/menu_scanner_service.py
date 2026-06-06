"""
AI Menu Scanner — OpenAI GPT-4o Vision + Google Cloud Vision OCR.

Flow:
  1. Client uploads menu image (base64)
  2. (Optional) Google Vision OCR extracts raw text
  3. GPT-4o Vision parses image into structured menu items
  4. Returns JSON array of menu items
"""
import base64
import io

import httpx
from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# OpenAI Vision accepts only these formats. Anything else (HEIC, PDF, TIFF, …)
# must be converted before upload or the upstream returns 400 invalid_image_format.
_OPENAI_ALLOWED_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp"}
# Cap dimension so OpenAI "detail: high" stays inside the 2048-tile budget.
# Keeps payload small enough that the 120 s read timeout (and Dio's shorter one)
# is comfortable.
_MAX_EDGE = 2048
_JPEG_QUALITY = 85

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

        # Normalize first — strip any data-URL prefix the client may have
        # included, sniff the actual format, reject unsupported types with a
        # clean 400, and downscale so the upstream Vision call stays fast.
        image_base64, mime_type = self._normalize_image(image_base64, mime_type)

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

    @staticmethod
    def _normalize_image(image_base64: str, claimed_mime: str) -> tuple[str, str]:
        """Decode → open with Pillow → re-encode as a small JPEG.

        - Strips a leading ``data:<mime>;base64,`` prefix if the client sent one.
        - Sniffs the real format via Pillow (the client-provided ``mime_type``
          is unreliable — Flutter and the web uploader both default to
          ``image/jpeg`` regardless of what the user actually picked).
        - Unsupported / unreadable formats (HEIC without a plugin, PDF, TIFF,
          corrupt bytes, …) raise a clean ``HTTPException(400)`` instead of
          letting OpenAI's ``invalid_image_format`` bubble up as a 400 with a
          confusing message or a 500.
        - Downscales to ``_MAX_EDGE`` on the longest side and re-encodes as
          JPEG q=85. Keeps the payload to OpenAI small so the upstream call
          fits inside the 120 s server timeout (and Dio's shorter mobile one).
        """
        if not image_base64:
            raise HTTPException(status_code=400, detail="image_base64 is empty")

        # 1) Strip optional data-URL prefix.
        s = image_base64.strip()
        if s.startswith("data:"):
            comma = s.find(",")
            if comma == -1:
                raise HTTPException(status_code=400, detail="Malformed data URL")
            s = s[comma + 1 :]

        # 2) base64-decode.
        try:
            raw = base64.b64decode(s, validate=False)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Invalid base64 payload: {exc}")

        if len(raw) < 32:
            raise HTTPException(status_code=400, detail="Image payload too small to be a valid image")

        # 3) Open + sniff via Pillow.
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except UnidentifiedImageError:
            # Common culprits: HEIC/HEIF (iPhone), PDF, raw camera files.
            head = raw[:12]
            hint = ""
            if b"ftypheic" in head or b"ftypmif1" in head or b"ftyphevc" in head:
                hint = " (looks like an HEIC/HEIF iPhone photo — please convert to JPG or PNG and try again)"
            elif head.startswith(b"%PDF"):
                hint = " (looks like a PDF — please upload an image of the menu page instead)"
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported image format. Allowed: JPG, PNG, GIF, WEBP.{hint}",
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Could not read image: {exc}")

        # 4) Normalize colour mode + downscale.
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        longest = max(w, h)
        if longest > _MAX_EDGE:
            scale = _MAX_EDGE / longest
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

        # 5) Re-encode as JPEG (always one of OpenAI's allowed formats).
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        out_bytes = buf.getvalue()
        out_b64 = base64.b64encode(out_bytes).decode("ascii")

        logger.info(
            "menu_scan_image_normalized",
            in_bytes=len(raw),
            out_bytes=len(out_bytes),
            in_size=(w, h),
            out_size=img.size,
            in_mime_claimed=claimed_mime,
            out_mime="image/jpeg",
        )
        return out_b64, "image/jpeg"

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
