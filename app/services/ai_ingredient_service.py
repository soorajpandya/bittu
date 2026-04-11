"""
AI Ingredient Mapping Service — OpenAI-powered recipe generation.

Given a menu item name (e.g. "Paneer Butter Masala"), returns a list of
raw material ingredients with approximate quantities.  Results are cached
in Redis so the same item_name → ingredients mapping is only computed once.
"""
import json
import httpx

from app.core.config import get_settings
from app.core.database import get_connection
from app.core.redis import cache_get, cache_set
from app.core.logging import get_logger

logger = get_logger(__name__)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

SYSTEM_PROMPT = """You are a professional Indian restaurant chef and food scientist.
Given a dish name, return a JSON array of raw material ingredients with approximate quantities needed to prepare ONE serving.

Rules:
- Include EVERY ingredient (oil, salt, spices, garnish, etc.)
- Use metric units: g, ml, kg, L, pieces
- Be realistic for a commercial kitchen (not home cooking)
- Use common raw-material names (e.g. "Refined Oil" not "Cooking Oil", "Green Chilli" not "Chilli")
- Return ONLY valid JSON. No markdown, no explanation.

Format:
[
  {"name": "Paneer", "quantity": 200, "unit": "g"},
  {"name": "Butter", "quantity": 30, "unit": "g"},
  ...
]"""


class AIIngredientService:

    async def suggest_ingredients(self, item_name: str) -> list[dict]:
        """Use OpenAI to suggest ingredients for a dish name."""
        settings = get_settings()
        if not settings.OPENAI_API_KEY:
            return []

        # Check Redis cache first
        cache_key = f"ai_ingredients:{item_name.lower().strip()}"
        cached = await cache_get(cache_key)
        if cached:
            return json.loads(cached)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                OPENAI_CHAT_URL,
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": item_name},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 1000,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        content = data["choices"][0]["message"]["content"].strip()
        # Strip markdown fences if present
        if content.startswith("```"):
            content = content.split("\n", 1)[1]
            content = content.rsplit("```", 1)[0]

        ingredients = json.loads(content)

        # Cache for 24h
        await cache_set(cache_key, json.dumps(ingredients), ttl=86400)
        logger.info("ai_ingredients_suggested", item=item_name, count=len(ingredients))
        return ingredients

    async def auto_link_ingredients(
        self,
        user_id: str,
        item_id: int,
        item_name: str,
    ) -> list[dict]:
        """
        AI-suggest ingredients for an item, match to existing raw materials,
        create missing ones, then link via item_ingredients table.
        Returns the linked ingredient rows.
        """
        suggestions = await self.suggest_ingredients(item_name)
        if not suggestions:
            return []

        linked = []
        async with get_connection() as conn:
            for s in suggestions:
                name = s["name"]
                qty = s.get("quantity", 0)
                unit = s.get("unit", "g")

                # Try to find existing ingredient by name (case-insensitive)
                row = await conn.fetchrow(
                    "SELECT id FROM ingredients WHERE user_id = $1 AND LOWER(name) = LOWER($2)",
                    user_id, name,
                )
                if row:
                    ingredient_id = row["id"]
                else:
                    # Create the raw material
                    row = await conn.fetchrow(
                        """
                        INSERT INTO ingredients (user_id, name, unit, current_stock, minimum_stock, cost_per_unit)
                        VALUES ($1, $2, $3, 0, 0, 0)
                        RETURNING id
                        """,
                        user_id, name, unit,
                    )
                    ingredient_id = row["id"]

                # Check if linkage already exists
                existing = await conn.fetchrow(
                    "SELECT id FROM item_ingredients WHERE user_id = $1 AND item_id = $2 AND ingredient_id = $3",
                    user_id, item_id, ingredient_id,
                )
                if existing:
                    linked.append({"ingredient_id": ingredient_id, "name": name, "quantity": qty, "unit": unit, "action": "exists"})
                    continue

                await conn.execute(
                    """
                    INSERT INTO item_ingredients (user_id, item_id, ingredient_id, quantity_used, unit)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    user_id, item_id, ingredient_id, qty, unit,
                )
                linked.append({"ingredient_id": ingredient_id, "name": name, "quantity": qty, "unit": unit, "action": "linked"})

        logger.info("ai_ingredients_linked", item_id=item_id, count=len(linked))
        return linked
