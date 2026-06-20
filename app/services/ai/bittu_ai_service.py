"""Bittu AI — assistant orchestrator.

Runs a manual OpenAI tool-calling loop over raw ``httpx`` (same pattern as
``app/services/ai_ingredient_service.py`` — no SDK). GPT chooses functions
from ``tool_schemas``; we execute them via ``metrics_toolbox`` against the
caller's tenant data and feed results back until the model produces a final
answer. The final answer is normalised to a structured shape the frontend can
render directly.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from app.core.auth import UserContext
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.ai.metrics_toolbox import run_tool
from app.services.ai.tool_schemas import TOOL_SCHEMAS

logger = get_logger(__name__)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
MAX_TOOL_ITERATIONS = 4

SYSTEM_PROMPT = """You are "Bittu AI", the virtual business manager for an Indian restaurant owner using Bittu POS.

You answer questions about THIS restaurant's performance using ONLY the data tools provided. Never invent numbers — if a tool returns no data or an unsupported flag, say so plainly. All money is in Indian Rupees (₹). Dates/periods are in IST.

How to work:
- Pick the most relevant tool(s) for the question and call them. You may call several.
- Be concise, practical and owner-friendly (no jargon). Think like a sharp restaurant manager.
- When you have enough data, STOP calling tools and produce the final answer.

FINAL ANSWER FORMAT — your last message MUST be a single JSON object with exactly these keys:
{
  "answer": "<one or two sentence direct answer with the key number(s)>",
  "explanation": "<short plain-language context: what drove it, comparisons, caveats>",
  "recommendations": ["<actionable suggestion>", "..."]
}
Return ONLY that JSON object in the final message (no markdown, no extra prose). recommendations may be an empty list."""


def _disabled_response(reason: str) -> dict:
    return {
        "answer": "Bittu AI is not available right now.",
        "explanation": reason,
        "recommendations": [],
        "metrics": [],
        "model": None,
    }


def _parse_final(content: str) -> dict:
    """Leniently parse the model's final JSON answer."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "answer" in obj:
            recs = obj.get("recommendations") or []
            if isinstance(recs, str):
                recs = [recs]
            return {
                "answer": str(obj.get("answer", "")).strip(),
                "explanation": str(obj.get("explanation", "")).strip(),
                "recommendations": [str(r) for r in recs],
            }
    except (json.JSONDecodeError, TypeError):
        pass
    # Fallback: treat whole content as the answer.
    return {"answer": text, "explanation": "", "recommendations": []}


class BittuAIService:
    """Natural-language business assistant with tool-calling."""

    async def ask(
        self,
        user: UserContext,
        question: str,
        history: Optional[list[dict]] = None,
    ) -> dict:
        settings = get_settings()
        if not settings.BITTU_AI_ENABLED:
            return _disabled_response("The assistant is disabled by configuration.")
        if not settings.OPENAI_API_KEY:
            return _disabled_response("OpenAI API key is not configured.")

        model = settings.BITTU_AI_MODEL or "gpt-4o-mini"

        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for h in (history or [])[-6:]:
            role = h.get("role")
            content = h.get("content")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": str(content)})
        messages.append({"role": "user", "content": question})

        collected: list[dict] = []

        async with httpx.AsyncClient(timeout=45) as client:
            headers = {
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            }

            for iteration in range(MAX_TOOL_ITERATIONS):
                use_tools = iteration < MAX_TOOL_ITERATIONS - 1
                payload: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": 0.2,
                }
                if use_tools:
                    payload["tools"] = TOOL_SCHEMAS
                    payload["tool_choice"] = "auto"

                resp = await client.post(OPENAI_CHAT_URL, headers=headers, json=payload)
                resp.raise_for_status()
                msg = resp.json()["choices"][0]["message"]

                tool_calls = msg.get("tool_calls") or []
                if not tool_calls:
                    final = _parse_final(msg.get("content") or "")
                    final["metrics"] = collected
                    final["model"] = model
                    logger.info(
                        "bittu_ai_answered",
                        user_id=user.user_id,
                        tools_used=len(collected),
                        iterations=iteration + 1,
                    )
                    return final

                # Append the assistant turn (with tool_calls) verbatim, then run tools.
                messages.append(msg)
                for call in tool_calls:
                    fn = call.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = await run_tool(name, user, args)
                    collected.append({"tool": name, "arguments": args, "result": result})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "content": json.dumps(result, default=str),
                        }
                    )

        # Exhausted iterations without a clean final message.
        return {
            "answer": "I gathered the data but couldn't finish summarising it.",
            "explanation": "Please try rephrasing the question.",
            "recommendations": [],
            "metrics": collected,
            "model": model,
        }


bittu_ai_service = BittuAIService()
