"""
Food Image Pipeline Service.

Handles AI image generation, resizing, storage, and caching for food items.
Global cache — images are shared across all restaurants.
"""
import asyncio
import base64
import io
import re
import time
from typing import Optional

import httpx
import structlog
from PIL import Image

from app.core.config import get_settings
from app.core.database import get_connection
from app.core.redis import cache_get, cache_set

logger = structlog.get_logger(__name__)

# ── Constants ──
MAX_BATCH_SIZE = 50
GENERATION_TIMEOUT = 300  # 5 minutes
OPENAI_IMAGE_TIMEOUT = 120  # per-image timeout
CACHE_TTL = 86400  # 24h Redis cache
MAX_CONCURRENT_GENERATIONS = 5  # rate-limit parallel OpenAI calls
STORAGE_BUCKET = "food-images"


def normalize_food_name(raw: str) -> str:
    """Strict normalization — must match Flutter + edge functions exactly."""
    name = raw.lower().strip()
    name = re.sub(r'[^a-z0-9\s]', '', name)
    name = re.sub(r'\s+', '_', name)
    return name


def _build_storage_url(settings, path: str) -> str:
    """Build public Supabase Storage URL."""
    return f"{settings.SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/{path}"


async def _upload_to_storage(
    client: httpx.AsyncClient,
    settings,
    path: str,
    data: bytes,
    content_type: str = "image/webp",
) -> str:
    """Upload file to Supabase Storage and return public URL."""
    url = f"{settings.SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{path}"
    resp = await client.post(
        url,
        content=data,
        headers={
            "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": content_type,
            "x-upsert": "true",
        },
    )
    if resp.status_code not in (200, 201):
        logger.error("storage_upload_failed", path=path, status=resp.status_code, body=resp.text[:200])
        raise RuntimeError(f"Storage upload failed: {resp.status_code}")
    return _build_storage_url(settings, path)


def _convert_to_webp(image_bytes: bytes, size: Optional[int] = None) -> bytes:
    """Convert image bytes to WebP, optionally resizing."""
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    if size:
        img = img.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=85)
    return buf.getvalue()


class FoodImageService:

    async def generate_batch(self, food_names: list[str]) -> dict:
        """
        Generate images for a batch of food names.
        Returns results keyed by raw food name (not normalized).
        """
        settings = get_settings()
        if not settings.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is not configured")

        if len(food_names) > MAX_BATCH_SIZE:
            raise ValueError(f"Max batch size is {MAX_BATCH_SIZE}, got {len(food_names)}")

        results = {}
        # Deduplicate while preserving raw names
        seen: dict[str, str] = {}  # normalized -> first raw name
        items_to_process: list[tuple[str, str]] = []  # (raw, normalized)

        for raw in food_names:
            normalized = normalize_food_name(raw)
            if not normalized:
                results[raw] = self._empty_result(raw, normalized)
                continue
            if normalized not in seen:
                seen[normalized] = raw
                items_to_process.append((raw, normalized))
            else:
                # Will copy result from first occurrence later
                pass

        # Check DB for all normalized names at once
        normalized_names = [n for _, n in items_to_process]
        cached_map = await self._bulk_lookup_db(normalized_names)

        # Separate cached vs need-generation
        to_generate: list[tuple[str, str]] = []
        for raw, normalized in items_to_process:
            if normalized in cached_map:
                row = cached_map[normalized]
                results[raw] = self._row_to_result(raw, normalized, row, cached=True)
            else:
                to_generate.append((raw, normalized))

        # Generate missing images with concurrency limit
        if to_generate:
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_GENERATIONS)
            tasks = [
                self._generate_single(raw, normalized, settings, semaphore)
                for raw, normalized in to_generate
            ]
            generated = await asyncio.gather(*tasks, return_exceptions=True)

            for (raw, normalized), result in zip(to_generate, generated):
                if isinstance(result, Exception):
                    logger.error("food_image_generation_failed", name=raw, error=str(result))
                    results[raw] = self._empty_result(raw, normalized)
                else:
                    results[raw] = result

        # Fill in duplicates (same normalized name, different raw name)
        for raw in food_names:
            if raw not in results:
                normalized = normalize_food_name(raw)
                first_raw = seen.get(normalized)
                if first_raw and first_raw in results:
                    # Copy result but with this raw name
                    copied = dict(results[first_raw])
                    copied["name"] = raw
                    results[raw] = copied

        return results

    async def bulk_lookup(self, names: list[str]) -> dict:
        """Look up existing food images by normalized names. Missing names are omitted."""
        rows = await self._bulk_lookup_db(names)
        results = {}
        for name, row in rows.items():
            results[name] = {
                "name": name,
                "image_url": row["image_url"],
                "image_original_url": row["image_original_url"],
                "image_512_url": row["image_512_url"],
                "image_256_url": row["image_256_url"],
            }
        return results

    async def single_lookup(self, name: str) -> Optional[dict]:
        """Look up a single food image by normalized name."""
        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM food_images WHERE name = $1", name
            )
        if not row:
            return None
        return {
            "name": row["name"],
            "image_url": row["image_url"],
            "image_original_url": row["image_original_url"],
            "image_512_url": row["image_512_url"],
            "image_256_url": row["image_256_url"],
        }

    # ── Internal helpers ──

    async def _bulk_lookup_db(self, names: list[str]) -> dict:
        """Fetch existing food_images rows for a list of normalized names."""
        if not names:
            return {}

        # Check Redis cache first
        cached_results = {}
        uncached_names = []
        for name in names:
            cache_key = f"food_img:{name}"
            cached = await cache_get(cache_key)
            if cached:
                import json
                cached_results[name] = json.loads(cached)
            else:
                uncached_names.append(name)

        if uncached_names:
            placeholders = ", ".join(f"${i+1}" for i in range(len(uncached_names)))
            async with get_connection() as conn:
                rows = await conn.fetch(
                    f"SELECT * FROM food_images WHERE name IN ({placeholders})",
                    *uncached_names,
                )
            for row in rows:
                row_dict = dict(row)
                # Remove datetime fields for JSON serialization
                row_dict.pop("created_at", None)
                row_dict.pop("updated_at", None)
                cached_results[row["name"]] = row_dict
                # Cache in Redis
                import json
                await cache_set(f"food_img:{row['name']}", json.dumps(row_dict), ttl=CACHE_TTL)

        return cached_results

    async def _generate_single(
        self,
        raw_name: str,
        normalized: str,
        settings,
        semaphore: asyncio.Semaphore,
    ) -> dict:
        """Generate a single food image via OpenAI, resize, upload, and store in DB."""
        async with semaphore:
            start_time = time.monotonic()

            async with httpx.AsyncClient(timeout=OPENAI_IMAGE_TIMEOUT) as client:
                # 1. Generate image via OpenAI
                prompt = (
                    f"A professional food photography shot of the dish called \"{raw_name}\". "
                    f"Show ONLY this specific dish: \"{raw_name}\". "
                    f"The dish must be clearly identifiable as {raw_name}, beautifully plated, "
                    f"top-down angle, clean background, restaurant-quality presentation, "
                    f"natural lighting, appetizing, high resolution."
                )
                resp = await client.post(
                    "https://api.openai.com/v1/images/generations",
                    headers={
                        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-image-1",
                        "prompt": prompt,
                        "n": 1,
                        "size": "1024x1024",
                        "quality": "auto",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            # 2. Decode image
            b64_data = data["data"][0]["b64_json"]
            raw_bytes = base64.b64decode(b64_data)

            # 3. Convert to WebP variants
            webp_original = _convert_to_webp(raw_bytes)
            webp_512 = _convert_to_webp(raw_bytes, size=512)
            webp_256 = _convert_to_webp(raw_bytes, size=256)

            # 4. Upload all variants to storage
            async with httpx.AsyncClient(timeout=30) as storage_client:
                original_url = await _upload_to_storage(
                    storage_client, settings, f"{normalized}/original.webp", webp_original
                )
                url_512 = await _upload_to_storage(
                    storage_client, settings, f"{normalized}/512.webp", webp_512
                )
                url_256 = await _upload_to_storage(
                    storage_client, settings, f"{normalized}/256.webp", webp_256
                )

            # 5. Store in DB
            async with get_connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO food_images (name, image_url, image_original_url, image_512_url, image_256_url)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (name) DO UPDATE SET
                        image_url = EXCLUDED.image_url,
                        image_original_url = EXCLUDED.image_original_url,
                        image_512_url = EXCLUDED.image_512_url,
                        image_256_url = EXCLUDED.image_256_url,
                        updated_at = now()
                    """,
                    normalized, original_url, original_url, url_512, url_256,
                )

            # 6. Cache in Redis
            import json
            row_dict = {
                "name": normalized,
                "image_url": original_url,
                "image_original_url": original_url,
                "image_512_url": url_512,
                "image_256_url": url_256,
            }
            await cache_set(f"food_img:{normalized}", json.dumps(row_dict), ttl=CACHE_TTL)

            elapsed = round((time.monotonic() - start_time) * 1000)
            logger.info(
                "food_image_generated",
                name=raw_name,
                normalized=normalized,
                time_ms=elapsed,
            )

            return {
                "name": raw_name,
                "normalized_name": normalized,
                "image_url": original_url,
                "image_original_url": original_url,
                "image_512_url": url_512,
                "image_256_url": url_256,
                "cached": False,
            }

    @staticmethod
    def _row_to_result(raw_name: str, normalized: str, row: dict, cached: bool) -> dict:
        return {
            "name": raw_name,
            "normalized_name": normalized,
            "image_url": row["image_url"],
            "image_original_url": row["image_original_url"],
            "image_512_url": row["image_512_url"],
            "image_256_url": row["image_256_url"],
            "cached": cached,
        }

    @staticmethod
    def _empty_result(raw_name: str, normalized: str) -> dict:
        return {
            "name": raw_name,
            "normalized_name": normalized,
            "image_url": "",
            "image_original_url": "",
            "image_512_url": "",
            "image_256_url": "",
            "cached": False,
        }
