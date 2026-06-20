"""Bittu AI — Revenue / Customer / Menu intelligence (Modules 3, 5, 7).

Assembles structured insight payloads from the same tenant-scoped readers the
assistant uses. Each payload is built from real metrics first (so the endpoint
is useful even with AI disabled), then optionally enriched with a short GPT
narrative. No new providers — reuses the existing httpx-to-OpenAI pattern.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from app.core.auth import UserContext
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.ai import metrics_toolbox as mt

logger = get_logger(__name__)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"


async def _narrate(title: str, data: dict[str, Any]) -> Optional[str]:
    """Ask GPT for a 2-3 sentence plain-language read of the metrics.

    Returns None when AI is unavailable so callers can degrade gracefully.
    """
    settings = get_settings()
    if not settings.BITTU_AI_ENABLED or not settings.OPENAI_API_KEY:
        return None
    model = settings.BITTU_AI_MODEL or "gpt-4o-mini"
    prompt = (
        f"You are Bittu AI, a restaurant business manager. In 2-3 short sentences, "
        f"give the owner a plain-language read of these {title} metrics (₹ INR). "
        f"Highlight what matters and one action. No markdown.\n\n"
        f"{json.dumps(data, default=str)}"
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
                    "temperature": 0.4,
                    "max_tokens": 220,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("bittu_ai_narrate_failed", title=title, error=str(exc))
        return None


class InsightsService:

    async def revenue_intelligence(self, user: UserContext, period: str = "this_month") -> dict:
        summary = await mt.get_sales_summary(user, period=period)
        trend = await mt.get_revenue_trend(user, period=period)
        peak = await mt.get_peak_hours(user, period=period)
        payload = {
            "period": summary["period"],
            "summary": summary,
            "trend": trend,
            "peak_hours": peak,
        }
        payload["ai_summary"] = await _narrate("revenue", {
            "summary": summary,
            "growth": trend.get("change"),
            "busiest_hour": peak.get("busiest_hour"),
        })
        return payload

    async def customer_intelligence(self, user: UserContext) -> dict:
        segments = await mt.get_customer_segments(user)
        inactive = await mt.get_inactive_customers(user, days=14)
        payload = {"segments": segments, "inactive": inactive}
        payload["ai_summary"] = await _narrate("customer", {
            "segments": segments.get("segments"),
            "total_customers": segments.get("total_customers"),
            "inactive_count": inactive.get("count"),
        })
        return payload

    async def menu_intelligence(self, user: UserContext, period: str = "this_month") -> dict:
        performance = await mt.get_menu_performance(user, period=period)
        top_units = await mt.get_top_items(user, period=period, by="units", limit=10)
        top_revenue = await mt.get_top_items(user, period=period, by="revenue", limit=10)
        payload = {
            "period": performance.get("period"),
            "performance": performance,
            "top_by_units": top_units,
            "top_by_revenue": top_revenue,
        }
        payload["ai_summary"] = await _narrate("menu", {
            "summary": performance.get("summary"),
            "top_by_units": top_units.get("items"),
            "top_by_revenue": top_revenue.get("items"),
        })
        return payload


insights_service = InsightsService()
