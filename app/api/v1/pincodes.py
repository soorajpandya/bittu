"""Deliverable Pincodes endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_role
from app.services.pincode_service import PincodeService

router = APIRouter(prefix="/pincodes", tags=["Deliverable Pincodes"])
_svc = PincodeService()


class PincodeCreate(BaseModel):
    pincode: str
    area_name: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None


@router.get("")
async def list_pincodes(
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.list_pincodes(user)


@router.post("", status_code=201)
async def create_pincode(
    body: PincodeCreate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.create_pincode(user, body.model_dump())


@router.delete("/{pincode_id}")
async def delete_pincode(
    pincode_id: int,
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.delete_pincode(user, pincode_id)
