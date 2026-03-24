"""Restaurant endpoints."""
import uuid as _uuid
from datetime import time as _time
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user
from app.core.database import get_connection, get_serializable_transaction
from app.core.exceptions import NotFoundError
from app.services.auth_service import _initialize_restaurant_and_branch
from app.core.logging import get_logger

router = APIRouter(prefix="/restaurants", tags=["Restaurants"])
logger = get_logger(__name__)


class CreateRestaurantIn(BaseModel):
    name: Optional[str] = None


class UpdateRestaurantIn(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    logo_url: Optional[str] = None
    cover_url: Optional[str] = None
    gst_number: Optional[str] = None
    fssai_number: Optional[str] = None
    is_active: Optional[bool] = None
    opening_time: Optional[str] = None
    closing_time: Optional[str] = None
    avg_prep_time: Optional[int] = None


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


@router.patch("/{restaurant_id}")
async def update_restaurant(
    restaurant_id: str,
    body: UpdateRestaurantIn,
    user: UserContext = Depends(get_current_user),
):
    """Partial-update a restaurant owned by the current user."""
    rid = _uuid.UUID(restaurant_id)
    owner_id = user.owner_id if user.is_branch_user else user.user_id
    data = body.model_dump(exclude_unset=True)

    async with get_serializable_transaction() as conn:
        existing = await conn.fetchrow(
            "SELECT * FROM restaurants WHERE id = $1 AND owner_id = $2 FOR UPDATE",
            rid,
            owner_id,
        )
        if not existing:
            raise NotFoundError("Restaurant", restaurant_id)

        fields = {k: v for k, v in data.items() if v is not None}
        if not fields:
            return dict(existing)

        # Convert time strings ("09:00") to datetime.time for asyncpg
        for tf in ("opening_time", "closing_time"):
            if tf in fields and isinstance(fields[tf], str):
                fields[tf] = _time.fromisoformat(fields[tf])

        set_parts = []
        vals = [rid, owner_id]
        for k, v in fields.items():
            vals.append(v)
            set_parts.append(f"{k} = ${len(vals)}")
        set_parts.append(f"updated_at = now()")

        row = await conn.fetchrow(
            f"UPDATE restaurants SET {', '.join(set_parts)} "
            f"WHERE id = $1 AND owner_id = $2 RETURNING *",
            *vals,
        )
    return dict(row)


@router.post("", status_code=201)
async def create_restaurant(
    body: CreateRestaurantIn = CreateRestaurantIn(),
    user: UserContext = Depends(get_current_user),
):
    """Create / initialize a restaurant for the current user (idempotent)."""
    result = await _initialize_restaurant_and_branch(user.user_id, email=user.email)
    return result
