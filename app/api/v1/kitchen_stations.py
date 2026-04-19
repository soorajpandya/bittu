"""Kitchen Stations endpoints."""
import uuid as _uuid
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.core.database import get_connection
from app.core.logging import get_logger

router = APIRouter(prefix="/kitchen-stations", tags=["Kitchen Stations"])
logger = get_logger(__name__)


class CreateKitchenStationIn(BaseModel):
    name: str


@router.get("")
async def list_kitchen_stations(
    is_active: Optional[bool] = Query(None),
    user: UserContext = Depends(require_permission("kitchen_station.read")),
):
    """List all kitchen stations for the current user's restaurant."""
    try:
        owner_id = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            if is_active is not None:
                rows = await conn.fetch(
                    "SELECT * FROM kitchen_stations WHERE user_id = $1 AND is_active = $2 ORDER BY name",
                    owner_id, is_active,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM kitchen_stations WHERE user_id = $1 ORDER BY name",
                    owner_id,
                )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("list_kitchen_stations_failed", error=str(e), user_id=user.user_id)
        return []


@router.post("", status_code=201)
async def create_kitchen_station(
    body: CreateKitchenStationIn,
    user: UserContext = Depends(require_permission("kitchen_station.manage")),
):
    """Create a new kitchen station."""
    owner_id = user.owner_id if user.is_branch_user else user.user_id
    station_id = str(_uuid.uuid4())
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO kitchen_stations (id, user_id, name, is_active, created_at)
            VALUES ($1, $2, $3, true, NOW())
            """,
            station_id, owner_id, body.name,
        )
        row = await conn.fetchrow(
            "SELECT * FROM kitchen_stations WHERE id = $1", station_id,
        )
    return dict(row)
