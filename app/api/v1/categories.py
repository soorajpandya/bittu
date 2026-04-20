"""Category Management endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.category_service import CategoryService

router = APIRouter(prefix="/categories", tags=["Categories"])
_svc = CategoryService()


class CategoryCreate(BaseModel):
    name: str
    slug: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    sort_order: Optional[int] = 0
    is_active: Optional[bool] = True


class CategoryUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


@router.get("")
async def list_categories(
    active_only: bool = False,
    user: UserContext = Depends(require_permission("menu.read")),
):
    return await _svc.list_categories(user, active_only=active_only)


@router.get("/{category_id}")
async def get_category(
    category_id: int,
    user: UserContext = Depends(require_permission("menu.write")),
):
    return await _svc.get_category(user, category_id)


@router.post("", status_code=201)
async def create_category(
    body: CategoryCreate,
    user: UserContext = Depends(require_permission("menu.write")),
):
    return await _svc.create_category(user, body.model_dump())


@router.patch("/{category_id}")
async def update_category(
    category_id: int,
    body: CategoryUpdate,
    user: UserContext = Depends(require_permission("menu.write")),
):
    return await _svc.update_category(user, category_id, body.model_dump(exclude_unset=True))


@router.delete("/{category_id}")
async def delete_category(
    category_id: int,
    user: UserContext = Depends(require_permission("menu.delete")),
):
    return await _svc.delete_category(user, category_id)
