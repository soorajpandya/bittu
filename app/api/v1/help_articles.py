"""Help Articles endpoints (public read-only)."""
from typing import Optional
from fastapi import APIRouter

from app.services.misc_service import HelpArticleService

router = APIRouter(prefix="/help", tags=["Help Articles"])
_svc = HelpArticleService()


@router.get("")
async def list_articles(category: Optional[str] = None):
    return await _svc.list_articles(category=category)


@router.get("/{article_id}")
async def get_article(article_id: int):
    return await _svc.get_article(article_id)
