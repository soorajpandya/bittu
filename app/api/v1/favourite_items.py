"""Favourite Items endpoints — alternate URL path for favourites."""
from fastapi import APIRouter, Depends

from app.core.auth import UserContext, get_current_user
from app.services.favourite_service import FavouriteService
from app.core.logging import get_logger

router = APIRouter(prefix="/favourite-items", tags=["Favourite Items"])
_svc = FavouriteService()
logger = get_logger(__name__)


@router.get("")
async def list_favourite_items(
    user: UserContext = Depends(get_current_user),
):
    """List favourite items for the current user."""
    try:
        return await _svc.list_favourites(user)
    except Exception as e:
        logger.warning("list_favourite_items_failed", error=str(e), user_id=user.user_id)
        return []
