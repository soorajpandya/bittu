"""Delivery Partners management endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.delivery_service import DeliveryService

router = APIRouter(prefix="/delivery-partners", tags=["Delivery Partners"])
_svc = DeliveryService()


class CreatePartnerIn(BaseModel):
    name: str
    phone: str


class UpdatePartnerIn(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("")
async def list_partners(
    user: UserContext = Depends(require_permission("delivery.view")),
):
    return await _svc.list_partners(user=user)


@router.post("")
async def create_partner(
    body: CreatePartnerIn,
    user: UserContext = Depends(require_permission("delivery.manage")),
):
    return await _svc.create_partner(user=user, name=body.name, phone=body.phone)


@router.patch("/{partner_id}")
async def update_partner(
    partner_id: str,
    body: UpdatePartnerIn,
    user: UserContext = Depends(require_permission("delivery.manage")),
):
    return await _svc.update_partner(
        user=user,
        partner_id=partner_id,
        name=body.name,
        phone=body.phone,
        is_active=body.is_active,
    )


@router.delete("/{partner_id}")
async def delete_partner(
    partner_id: str,
    user: UserContext = Depends(require_permission("delivery.manage")),
):
    return await _svc.delete_partner(user=user, partner_id=partner_id)
