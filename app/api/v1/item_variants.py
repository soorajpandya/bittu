"""Item Variants endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.item_customization_service import ItemVariantService

router = APIRouter(prefix="/item-variants", tags=["Item Variants"])
_svc = ItemVariantService()


class VariantCreate(BaseModel):
    item_id: int
    name: str
    price: float
    is_active: Optional[bool] = True
    sku: Optional[str] = None


class VariantUpdate(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    is_active: Optional[bool] = None
    sku: Optional[str] = None


@router.get("")
async def list_variants(
    item_id: Optional[int] = None,
    user: UserContext = Depends(require_permission("menu.read")),
):
    return await _svc.list_variants(user, item_id=item_id)


@router.post("", status_code=201)
async def create_variant(
    body: VariantCreate,
    user: UserContext = Depends(require_permission("menu.write")),
):
    return await _svc.create_variant(user, body.model_dump())


@router.patch("/{variant_id}")
async def update_variant(
    variant_id: int,
    body: VariantUpdate,
    user: UserContext = Depends(require_permission("menu.write")),
):
    return await _svc.update_variant(user, variant_id, body.model_dump(exclude_unset=True))


@router.delete("/{variant_id}")
async def delete_variant(
    variant_id: int,
    user: UserContext = Depends(require_permission("menu.delete")),
):
    return await _svc.delete_variant(user, variant_id)
