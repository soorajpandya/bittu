"""Item Extras endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_role
from app.services.item_customization_service import ItemExtraService

router = APIRouter(prefix="/item-extras", tags=["Item Extras"])
_svc = ItemExtraService()


class ExtraCreate(BaseModel):
    item_id: int
    name: str
    price: float
    is_active: Optional[bool] = True


class ExtraUpdate(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    is_active: Optional[bool] = None


@router.get("")
async def list_extras(
    item_id: Optional[int] = None,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.list_extras(user, item_id=item_id)


@router.post("", status_code=201)
async def create_extra(
    body: ExtraCreate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.create_extra(user, body.model_dump())


@router.patch("/{extra_id}")
async def update_extra(
    extra_id: int,
    body: ExtraUpdate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.update_extra(user, extra_id, body.model_dump(exclude_unset=True))


@router.delete("/{extra_id}")
async def delete_extra(
    extra_id: int,
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.delete_extra(user, extra_id)
