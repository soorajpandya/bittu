"""Customer Management endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.customer_service import CustomerService

router = APIRouter(prefix="/customers", tags=["Customers"])
_svc = CustomerService()


class CustomerCreate(BaseModel):
    name: str
    email: Optional[str] = None
    phone_number: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None


class CustomerUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone_number: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None


@router.get("")
async def list_customers(
    search: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("customer.read")),
):
    return await _svc.list_customers(user, search=search, limit=limit, offset=offset)


@router.get("/{customer_id}")
async def get_customer(
    customer_id: int,
    user: UserContext = Depends(require_permission("customer.read")),
):
    return await _svc.get_customer(user, customer_id)


@router.post("", status_code=201)
async def create_customer(
    body: CustomerCreate,
    user: UserContext = Depends(require_permission("customer.write")),
):
    return await _svc.create_customer(user, body.model_dump())


@router.patch("/{customer_id}")
async def update_customer(
    customer_id: int,
    body: CustomerUpdate,
    user: UserContext = Depends(require_permission("customer.write")),
):
    return await _svc.update_customer(user, customer_id, body.model_dump(exclude_unset=True))


@router.delete("/{customer_id}")
async def delete_customer(
    customer_id: int,
    user: UserContext = Depends(require_permission("customer.delete")),
):
    return await _svc.delete_customer(user, customer_id)
