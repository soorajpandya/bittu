"""Item Addons endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.item_customization_service import ItemAddonService

router = APIRouter(prefix="/item-addons", tags=["Item Addons"])
_svc = ItemAddonService()


class AddonCreate(BaseModel):
    item_id: int
    name: str
    price: float
    is_active: Optional[bool] = True


class AddonUpdate(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    is_active: Optional[bool] = None


@router.get("")
async def list_addons(
    item_id: Optional[int] = None,
    user: UserContext = Depends(require_permission("menu.read")),
):
    return await _svc.list_addons(user, item_id=item_id)


@router.post("", status_code=201)
async def create_addon(
    body: AddonCreate,
    user: UserContext = Depends(require_permission("menu.write")),
):
    return await _svc.create_addon(user, body.model_dump())


@router.patch("/{addon_id}")
async def update_addon(
    addon_id: int,
    body: AddonUpdate,
    user: UserContext = Depends(require_permission("menu.write")),
):
    return await _svc.update_addon(user, addon_id, body.model_dump(exclude_unset=True))


@router.delete("/{addon_id}")
async def delete_addon(
    addon_id: int,
    user: UserContext = Depends(require_permission("menu.delete")),
):
    return await _svc.delete_addon(user, addon_id)
