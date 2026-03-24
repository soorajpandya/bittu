"""Modifier Groups top-level alias — delegates to ModifierService."""
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_role
from app.services.modifier_service import ModifierService

router = APIRouter(prefix="/modifier-groups", tags=["Modifier Groups"])
_svc = ModifierService()


class OptionIn(BaseModel):
    name: str
    price: Optional[float] = 0
    is_active: Optional[bool] = True


class GroupCreate(BaseModel):
    name: str
    is_required: Optional[bool] = False
    min_selections: Optional[int] = 0
    max_selections: Optional[int] = None
    options: Optional[list[OptionIn]] = []


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    is_required: Optional[bool] = None
    min_selections: Optional[int] = None
    max_selections: Optional[int] = None


class OptionUpdate(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    is_active: Optional[bool] = None


@router.get("")
async def list_groups(
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.list_groups(user)


@router.get("/{group_id}")
async def get_group(
    group_id: int,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.get_group(user, group_id)


@router.post("", status_code=201)
async def create_group(
    body: GroupCreate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    data = body.model_dump()
    data["options"] = [o.model_dump() for o in body.options] if body.options else []
    return await _svc.create_group(user, data)


@router.patch("/{group_id}")
async def update_group(
    group_id: int,
    body: GroupUpdate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.update_group(user, group_id, body.model_dump(exclude_unset=True))


@router.delete("/{group_id}")
async def delete_group(
    group_id: int,
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.delete_group(user, group_id)


@router.post("/{group_id}/options", status_code=201)
async def add_option(
    group_id: int,
    body: OptionIn,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.add_option(user, group_id, body.model_dump())


@router.patch("/options/{option_id}")
async def update_option(
    option_id: int,
    body: OptionUpdate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.update_option(user, option_id, body.model_dump(exclude_unset=True))


@router.delete("/options/{option_id}")
async def delete_option(
    option_id: int,
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.delete_option(user, option_id)
