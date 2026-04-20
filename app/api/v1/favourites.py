"""Favourite Items endpoints."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.favourite_service import FavouriteService

router = APIRouter(prefix="/favourites", tags=["Favourites"])
_svc = FavouriteService()


class FavouriteIn(BaseModel):
    item_id: int


@router.get("")
async def list_favourites(
    user: UserContext = Depends(require_permission("favourites.manage")),
):
    return await _svc.list_favourites(user)


@router.post("", status_code=201)
async def add_favourite(
    body: FavouriteIn,
    user: UserContext = Depends(require_permission("favourites.manage")),
):
    return await _svc.add_favourite(user, body.item_id)


@router.delete("/{item_id}")
async def remove_favourite(
    item_id: int,
    user: UserContext = Depends(require_permission("favourites.manage")),
):
    return await _svc.remove_favourite(user, item_id)
