"""Item Station Mapping endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.item_station_service import ItemStationService

router = APIRouter(prefix="/item-stations", tags=["Item Station Mapping"])
_svc = ItemStationService()


class MappingCreate(BaseModel):
    item_id: int
    station_id: int


@router.get("")
async def list_mappings(
    item_id: Optional[int] = None,
    station_id: Optional[int] = None,
    user: UserContext = Depends(require_permission("menu.read")),
):
    return await _svc.list_mappings(user, item_id=item_id, station_id=station_id)


@router.post("", status_code=201)
async def create_mapping(
    body: MappingCreate,
    user: UserContext = Depends(require_permission("menu.write")),
):
    return await _svc.create_mapping(user, body.item_id, body.station_id)


@router.delete("")
async def delete_mapping(
    item_id: int,
    station_id: int,
    user: UserContext = Depends(require_permission("menu.write")),
):
    return await _svc.delete_mapping(user, item_id, station_id)
