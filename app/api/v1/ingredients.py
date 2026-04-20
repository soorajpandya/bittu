"""Ingredients endpoints — standalone ingredient list."""
from typing import Optional
from fastapi import APIRouter, Depends, Query

from app.core.auth import UserContext, get_current_user
from app.core.database import get_connection
from app.core.logging import get_logger

router = APIRouter(prefix="/ingredients", tags=["Ingredients"])
logger = get_logger(__name__)


@router.get("")
async def list_ingredients(
    user: UserContext = Depends(get_current_user),
):
    """List all ingredients for the current user's restaurant."""
    try:
        owner_id = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            rows = await conn.fetch(
                "SELECT * FROM ingredients WHERE user_id = $1 ORDER BY name",
                owner_id,
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("list_ingredients_failed", error=str(e), user_id=user.user_id)
        return []
