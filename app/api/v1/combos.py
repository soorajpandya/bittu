"""Combo Management endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_role
from app.services.combo_service import ComboService

router = APIRouter(prefix="/combos", tags=["Combos"])
_svc = ComboService()


class ComboItemIn(BaseModel):
    item_id: int
    quantity: Optional[int] = 1


class ComboCreate(BaseModel):
    name: str
    description: Optional[str] = None
    price: float
    image_url: Optional[str] = None
    is_active: Optional[bool] = True
    items: Optional[list[ComboItemIn]] = []


class ComboUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    image_url: Optional[str] = None
    is_active: Optional[bool] = None
    items: Optional[list[ComboItemIn]] = None


@router.get("")
async def list_combos(
    active_only: bool = False,
    user: UserContext = Depends(require_role("owner", "manager", "cashier")),
):
    return await _svc.list_combos(user, active_only=active_only)


@router.get("/{combo_id}")
async def get_combo(
    combo_id: int,
    user: UserContext = Depends(require_role("owner", "manager", "cashier")),
):
    return await _svc.get_combo(user, combo_id)


@router.post("", status_code=201)
async def create_combo(
    body: ComboCreate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    data = body.model_dump()
    data["items"] = [i.model_dump() for i in body.items] if body.items else []
    return await _svc.create_combo(user, data)


@router.patch("/{combo_id}")
async def update_combo(
    combo_id: int,
    body: ComboUpdate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    data = body.model_dump(exclude_unset=True)
    if "items" in data and data["items"] is not None:
        data["items"] = [i.model_dump() if hasattr(i, 'model_dump') else i for i in data["items"]]
    return await _svc.update_combo(user, combo_id, data)


@router.delete("/{combo_id}")
async def delete_combo(
    combo_id: int,
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.delete_combo(user, combo_id)
