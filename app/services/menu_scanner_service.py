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

SYSTEM_PROMPT = """You are a restaurant menu parser. Analyze the provided menu image and extract ALL menu items.
Return a valid JSON array where each item has:
- "item_name": string (exact name from menu)
- "price": number (in INR, 0 if not visible)
- "category": string (category/section heading)
- "is_veg": boolean (true if marked vegetarian, null if unknown)
- "spice_level": string ("mild"|"medium"|"hot"|null)
- "prep_time_min": number (estimated minutes, null if unknown)
- "short_code": string (first 3 chars uppercase of item name)
- "description": string (description if present, else empty)

Return ONLY the JSON array, no markdown fences or explanation."""


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
        return {"items": items, "ocr_text": ocr_text}

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
                "text": f"OCR text extracted from this menu:\n{ocr_text}",
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
