"""Deliverable Pincodes endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.core.cache import cached_route, invalidate_prefix
from app.services.pincode_service import PincodeService

router = APIRouter(prefix="/pincodes", tags=["Delivery"])
_svc = PincodeService()
_CACHE_PREFIX = "pincodes"


class PincodeCreate(BaseModel):
    pincode: str
    area_name: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None


@router.get("")
@cached_route(prefix=_CACHE_PREFIX, ttl=600)
async def list_pincodes(
    user: UserContext = Depends(require_permission("delivery.read")),
):
    return await _svc.list_pincodes(user)


@router.post("", status_code=201)
async def create_pincode(
    body: PincodeCreate,
    user: UserContext = Depends(require_permission("delivery.write")),
):
    result = await _svc.create_pincode(user, body.model_dump())
    await invalidate_prefix(_CACHE_PREFIX, user)
    return result


@router.delete("/{pincode_id}")
async def delete_pincode(
    pincode_id: int,
    user: UserContext = Depends(require_permission("delivery.delete")),
):
    result = await _svc.delete_pincode(user, pincode_id)
    await invalidate_prefix(_CACHE_PREFIX, user)
    return result
