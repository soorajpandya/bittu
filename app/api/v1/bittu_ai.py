"""Bittu AI — Restaurant Operating Intelligence Layer (v1) API.

Endpoints (all JWT-gated, owner/manager via the ``analytics.read`` permission):
    POST /api/v1/bittu-ai/ask                — natural-language business Q&A
    GET  /api/v1/bittu-ai/briefing           — today's daily briefing (cached)
    GET  /api/v1/bittu-ai/insights/revenue   — revenue intelligence
    GET  /api/v1/bittu-ai/insights/customers — customer intelligence
    GET  /api/v1/bittu-ai/insights/menu      — menu intelligence

All data is tenant-scoped through the existing RLS/connection stack; the AI
never sees raw SQL or cross-tenant data.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.services.ai.bittu_ai_service import bittu_ai_service
from app.services.ai.briefing_service import briefing_service
from app.services.ai.insights_service import insights_service

router = APIRouter(prefix="/bittu-ai", tags=["Bittu AI"])

_PERIODS = {
    "today", "yesterday", "this_week", "last_week",
    "this_month", "last_month", "last_7_days", "last_30_days",
}


class ChatTurn(BaseModel):
    role: str = Field(..., description="'user' or 'assistant'")
    content: str


class AskIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000)
    history: Optional[list[ChatTurn]] = Field(
        default=None, description="Optional prior turns for follow-up context."
    )


class AskOut(BaseModel):
    answer: str
    explanation: str = ""
    recommendations: list[str] = []
    metrics: list[dict] = []
    model: Optional[str] = None


def _norm_period(period: Optional[str], default: str) -> str:
    p = (period or default).strip().lower()
    return p if p in _PERIODS else default


@router.post("/ask", response_model=AskOut)
async def ask(
    body: AskIn,
    user: UserContext = Depends(require_permission("analytics.read")),
):
    """Ask Bittu AI a question about the business; answers from real data."""
    history = [t.model_dump() for t in body.history] if body.history else None
    result = await bittu_ai_service.ask(user, body.question, history=history)
    return result


@router.get("/briefing")
async def briefing(
    refresh: bool = Query(False, description="Bypass the daily cache and recompute."),
    user: UserContext = Depends(require_permission("analytics.read")),
):
    """Today's daily briefing about yesterday's performance (cached per day)."""
    return await briefing_service.get_briefing(user, force_refresh=refresh)


@router.get("/insights/revenue")
async def insights_revenue(
    period: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("analytics.read")),
):
    return await insights_service.revenue_intelligence(user, period=_norm_period(period, "this_month"))


@router.get("/insights/customers")
async def insights_customers(
    user: UserContext = Depends(require_permission("analytics.read")),
):
    return await insights_service.customer_intelligence(user)


@router.get("/insights/menu")
async def insights_menu(
    period: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("analytics.read")),
):
    return await insights_service.menu_intelligence(user, period=_norm_period(period, "this_month"))
