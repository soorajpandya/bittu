"""Bittu AI — Daily Briefing (Module 2).

Assembles yesterday's headline metrics for the restaurant and asks GPT to
narrate a short morning briefing. Cached per restaurant + IST date in Redis
(reusing ``cache_get``/``cache_set`` like ``ai_ingredient_service``), so it is
computed at most once per day. On-demand — no scheduler in v1.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from app.core.auth import UserContext
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.ist import ist_today
from app.core.redis import cache_get, cache_set
from app.services.ai import metrics_toolbox as mt

logger = get_logger(__name__)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
_CACHE_TTL = 6 * 3600  # refresh a few times a day at most


def _scope(user: UserContext) -> str:
    return user.restaurant_id or (user.owner_id if user.is_branch_user else user.user_id)


async def _narrate_briefing(metrics: dict[str, Any]) -> Optional[str]:
    settings = get_settings()
    if not settings.BITTU_AI_ENABLED or not settings.OPENAI_API_KEY:
        return None
    model = settings.BITTU_AI_MODEL or "gpt-4o-mini"
    prompt = (
        "You are Bittu AI, a restaurant business manager. Write a warm, concise "
        "morning briefing (3-4 sentences) for the owner about YESTERDAY, using "
        "these metrics (₹ INR). Mention revenue, how it compares to the day "
        "before, the best-selling item, and flag anything needing attention "
        "(low stock, inactive customers). End with one focused suggestion for "
        "today. No markdown.\n\n"
        f"{json.dumps(metrics, default=str)}"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                OPENAI_CHAT_URL,
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.5,
                    "max_tokens": 260,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("bittu_ai_briefing_narrate_failed", error=str(exc))
        return None


def _fallback_text(m: dict[str, Any]) -> str:
    top = m.get("top_item") or {}
    parts = [
        f"Yesterday you did ₹{m.get('revenue', 0):,.0f} across {m.get('orders', 0)} orders "
        f"(avg ₹{m.get('average_order_value', 0):,.0f})."
    ]
    chg = m.get("revenue_change_pct")
    if chg is not None:
        direction = "up" if chg >= 0 else "down"
        parts.append(f"That's {direction} {abs(chg):.0f}% vs the day before.")
    if top.get("name"):
        parts.append(f"Top seller was {top['name']} ({top.get('units_sold', 0)} sold).")
    if m.get("low_stock_count"):
        parts.append(f"{m['low_stock_count']} ingredient(s) are low on stock.")
    return " ".join(parts)


class BriefingService:

    async def get_briefing(self, user: UserContext, force_refresh: bool = False) -> dict:
        today = ist_today()
        cache_key = f"bittu_ai:briefing:{_scope(user)}:{today}"
        if not force_refresh:
            cached = await cache_get(cache_key)
            if cached:
                try:
                    return json.loads(cached)
                except json.JSONDecodeError:
                    pass

        sales = await mt.get_sales_summary(user, period="yesterday")
        trend = await mt.get_revenue_trend(user, period="yesterday")
        top = await mt.get_top_items(user, period="yesterday", by="units", limit=1)
        alerts = await mt.get_inventory_alerts(user)
        inactive = await mt.get_inactive_customers(user, days=14, limit=1)

        top_item = (top.get("items") or [{}])[0] if top.get("items") else {}
        metrics = {
            "date": str(today),
            "revenue": sales["revenue"],
            "orders": sales["orders"],
            "average_order_value": sales["average_order_value"],
            "revenue_change_pct": trend.get("change", {}).get("revenue_pct"),
            "top_item": {
                "name": top_item.get("name"),
                "units_sold": top_item.get("units_sold"),
            },
            "low_stock_count": alerts.get("low_stock_count", 0),
            "inactive_customer_count": inactive.get("count", 0),
        }

        narrative = await _narrate_briefing(metrics)
        result = {
            "date": str(today),
            "briefing": narrative or _fallback_text(metrics),
            "ai_generated": narrative is not None,
            "metrics": metrics,
        }

        try:
            await cache_set(cache_key, json.dumps(result, default=str), ttl=_CACHE_TTL)
        except Exception:  # noqa: BLE001
            pass
        return result


briefing_service = BriefingService()
