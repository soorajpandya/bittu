"""Item Ingredients endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_role
from app.services.item_ingredient_service import ItemIngredientService

router = APIRouter(prefix="/item-ingredients", tags=["Item Ingredients"])
_svc = ItemIngredientService()


class IngredientLink(BaseModel):
    item_id: int
    ingredient_id: int
    quantity_used: Optional[float] = 0
    unit: Optional[str] = None


class IngredientLinkUpdate(BaseModel):
    quantity_used: Optional[float] = None
    unit: Optional[str] = None


@router.get("")
async def list_ingredients(
    item_id: Optional[int] = None,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.list_ingredients(user, item_id=item_id)


@router.post("", status_code=201)
async def add_ingredient(
    body: IngredientLink,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.add_ingredient(user, body.model_dump())


@router.patch("/{ii_id}")
async def update_ingredient(
    ii_id: int,
    body: IngredientLinkUpdate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.update_ingredient(user, ii_id, body.model_dump(exclude_unset=True))


@router.delete("/{ii_id}")
async def remove_ingredient(
    ii_id: int,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.remove_ingredient(user, ii_id)
