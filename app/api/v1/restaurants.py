"""Restaurant endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user
from app.core.database import get_connection
from app.services.auth_service import _initialize_restaurant_and_branch
from app.core.logging import get_logger

router = APIRouter(prefix="/restaurants", tags=["Restaurants"])
logger = get_logger(__name__)


class CreateRestaurantIn(BaseModel):
    name: Optional[str] = None


@router.get("")
async def get_restaurants(
    user: UserContext = Depends(get_current_user),
):
    """Return the current user's restaurant(s)."""
    owner_id = user.owner_id if user.is_branch_user else user.user_id
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT r.*, sb.id as main_branch_id, sb.name as main_branch_name
            FROM restaurants r
            LEFT JOIN sub_branches sb ON sb.restaurant_id = r.id AND sb.is_main_branch = true
            WHERE r.owner_id = $1
            ORDER BY r.created_at DESC
            """,
            owner_id,
        )
        return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_restaurant(
    body: CreateRestaurantIn = CreateRestaurantIn(),
    user: UserContext = Depends(get_current_user),
):
    """Create / initialize a restaurant for the current user (idempotent)."""
    result = await _initialize_restaurant_and_branch(user.user_id, email=user.email)
    return result
