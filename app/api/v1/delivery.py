"""Delivery & Tracking endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.delivery_service import DeliveryService

router = APIRouter(prefix="/delivery", tags=["Delivery"])
_svc = DeliveryService()


class CreateDeliveryIn(BaseModel):
    order_id: str
    branch_id: str
    address: str
    customer_phone: str
    customer_name: Optional[str] = None


class AssignPartnerIn(BaseModel):
    partner_id: str


class UpdateDeliveryStatusIn(BaseModel):
    status: str


class UpdateLocationIn(BaseModel):
    latitude: float
    longitude: float


@router.post("")
async def create_delivery(
    body: CreateDeliveryIn,
    user: UserContext = Depends(require_permission("delivery.create")),
):
    return await _svc.create_delivery(
        user=user,
        order_id=body.order_id,
        branch_id=body.branch_id,
        address=body.address,
        customer_phone=body.customer_phone,
        customer_name=body.customer_name,
    )


@router.patch("/{delivery_id}/assign")
async def assign_partner(
    delivery_id: str,
    body: AssignPartnerIn,
    user: UserContext = Depends(require_permission("delivery.manage")),
):
    return await _svc.assign_partner(
        user=user,
        delivery_id=delivery_id,
        partner_id=body.partner_id,
    )


@router.patch("/{delivery_id}/status")
async def update_delivery_status(
    delivery_id: str,
    body: UpdateDeliveryStatusIn,
    user: UserContext = Depends(require_permission("delivery.manage")),
):
    return await _svc.update_status(
        user=user,
        delivery_id=delivery_id,
        new_status=body.status,
    )


@router.post("/{delivery_id}/location")
async def update_location(
    delivery_id: str,
    body: UpdateLocationIn,
    user: UserContext = Depends(require_permission("delivery.manage")),
):
    return await _svc.update_location(
        delivery_id=delivery_id,
        latitude=body.latitude,
        longitude=body.longitude,
    )
