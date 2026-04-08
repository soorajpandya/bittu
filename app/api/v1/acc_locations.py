"""Locations CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/locations", tags=["Accounting – Locations"])

TABLE = "acc_locations"
PK = "location_id"
LABEL = "Location"


_auth = require_permission("accounting:read")


class LocationCreate(BaseModel):
    location_name: str
    address: Optional[dict] = None
    phone: Optional[str] = None
    fax: Optional[str] = None
    is_primary: bool = False
    custom_fields: Optional[list] = None


class LocationUpdate(BaseModel):
    location_name: Optional[str] = None
    address: Optional[dict] = None
    phone: Optional[str] = None
    fax: Optional[str] = None
    is_primary: Optional[bool] = None
    custom_fields: Optional[list] = None


@router.get("")
async def list_locations(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, page=page, per_page=per_page, search_fields=["location_name"])


@router.post("", status_code=201)
async def create_location(body: LocationCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{location_id}")
async def get_location(location_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, location_id, user, LABEL)


@router.put("/{location_id}")
async def update_location(location_id: UUID, body: LocationUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, location_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{location_id}")
async def delete_location(location_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, location_id, user, LABEL)
