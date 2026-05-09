"""Offer Management endpoints."""
from typing import Optional
from datetime import datetime
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.core.cache import cached_route, invalidate_prefix
from app.services.offer_service import OfferService

router = APIRouter(prefix="/offers", tags=["Offers"])
_svc = OfferService()
_CACHE_PREFIX = "offers"


class OfferCreate(BaseModel):
    title: str
    description: Optional[str] = None
    discount: Optional[float] = 0
    code: Optional[str] = None
    type: Optional[str] = "percentage"
    icon: Optional[str] = None
    expiry_days: Optional[int] = None
    is_active: Optional[bool] = True
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None


class OfferUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    discount: Optional[float] = None
    code: Optional[str] = None
    type: Optional[str] = None
    icon: Optional[str] = None
    expiry_days: Optional[int] = None
    is_active: Optional[bool] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None


@router.get("")
@cached_route(prefix=_CACHE_PREFIX, ttl=120)
async def list_offers(
    active_only: bool = False,
    user: UserContext = Depends(require_permission("promotion.read")),
):
    return await _svc.list_offers(user, active_only=active_only)


@router.get("/{offer_id}")
@cached_route(prefix=_CACHE_PREFIX, ttl=120)
async def get_offer(
    offer_id: int,
    user: UserContext = Depends(require_permission("promotion.read")),
):
    return await _svc.get_offer(user, offer_id)


@router.post("", status_code=201)
async def create_offer(
    body: OfferCreate,
    user: UserContext = Depends(require_permission("promotion.write")),
):
    result = await _svc.create_offer(user, body.model_dump())
    await invalidate_prefix(_CACHE_PREFIX, user)
    return result


@router.patch("/{offer_id}")
async def update_offer(
    offer_id: int,
    body: OfferUpdate,
    user: UserContext = Depends(require_permission("promotion.write")),
):
    result = await _svc.update_offer(user, offer_id, body.model_dump(exclude_unset=True))
    await invalidate_prefix(_CACHE_PREFIX, user)
    return result


@router.delete("/{offer_id}")
async def delete_offer(
    offer_id: int,
    user: UserContext = Depends(require_permission("promotion.delete")),
):
    result = await _svc.delete_offer(user, offer_id)
    await invalidate_prefix(_CACHE_PREFIX, user)
    return result
