"""Item Ingredients endpoints."""
from typing import Optional, Union
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.item_ingredient_service import ItemIngredientService

router = APIRouter(prefix="/item-ingredients", tags=["Menu"])
_svc = ItemIngredientService()


class IngredientLink(BaseModel):
    item_id: int
    # ingredients.id is TEXT in the DB but the frontend often sends a numeric
    # primary key (e.g. 93). Accept either and let the service stringify.
    ingredient_id: Union[int, str]
    quantity_used: Optional[float] = 0
    unit: Optional[str] = None


class IngredientLinkUpdate(BaseModel):
    quantity_used: Optional[float] = None
    unit: Optional[str] = None


@router.get("")
async def list_ingredients(
    item_id: Optional[int] = None,
    user: UserContext = Depends(require_permission("menu.read")),
):
    return await _svc.list_ingredients(user, item_id=item_id)


@router.post("", status_code=201)
async def add_ingredient(
    body: IngredientLink,
    user: UserContext = Depends(require_permission("menu.write")),
):
    return await _svc.add_ingredient(user, body.model_dump())


@router.patch("/{ii_id}")
async def update_ingredient(
    ii_id: int,
    body: IngredientLinkUpdate,
    user: UserContext = Depends(require_permission("menu.write")),
):
    return await _svc.update_ingredient(user, ii_id, body.model_dump(exclude_unset=True))


@router.delete("/{ii_id}")
async def remove_ingredient(
    ii_id: int,
    user: UserContext = Depends(require_permission("menu.write")),
):
    return await _svc.remove_ingredient(user, ii_id)
