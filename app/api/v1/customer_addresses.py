"""Customer Address endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.customer_address_service import CustomerAddressService

router = APIRouter(prefix="/customer-addresses", tags=["Customer Addresses"])
_svc = CustomerAddressService()


class AddressCreate(BaseModel):
    label: Optional[str] = "Home"
    address_line: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    is_default: Optional[bool] = False


class AddressUpdate(BaseModel):
    label: Optional[str] = None
    address_line: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    is_default: Optional[bool] = None


@router.get("/{customer_id}")
async def list_addresses(
    customer_id: int,
    user: UserContext = Depends(require_permission("customer.read")),
):
    return await _svc.list_addresses(user, customer_id)


@router.post("/{customer_id}", status_code=201)
async def create_address(
    customer_id: int,
    body: AddressCreate,
    user: UserContext = Depends(require_permission("customer.write")),
):
    return await _svc.create_address(user, customer_id, body.model_dump())


@router.patch("/address/{address_id}")
async def update_address(
    address_id: int,
    body: AddressUpdate,
    user: UserContext = Depends(require_permission("customer.write")),
):
    return await _svc.update_address(user, address_id, body.model_dump(exclude_unset=True))


@router.delete("/address/{address_id}")
async def delete_address(
    address_id: int,
    user: UserContext = Depends(require_permission("customer.write")),
):
    return await _svc.delete_address(user, address_id)
