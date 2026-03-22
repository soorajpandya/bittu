"""Restaurant endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user
from app.services.auth_service import _initialize_restaurant_and_branch
from app.core.logging import get_logger

router = APIRouter(prefix="/restaurants", tags=["Restaurants"])
logger = get_logger(__name__)


class CreateRestaurantIn(BaseModel):
    name: Optional[str] = None


@router.post("", status_code=201)
async def create_restaurant(
    body: CreateRestaurantIn = CreateRestaurantIn(),
    user: UserContext = Depends(get_current_user),
):
    """Create / initialize a restaurant for the current user (idempotent)."""
    result = await _initialize_restaurant_and_branch(user.user_id, email=user.email)
    return result
