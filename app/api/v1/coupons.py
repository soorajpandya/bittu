"""Coupon Management endpoints."""
from typing import Optional
from datetime import datetime
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.coupon_service import CouponService

router = APIRouter(prefix="/coupons", tags=["Coupons"])
_svc = CouponService()


class CouponCreate(BaseModel):
    code: str
    title: Optional[str] = None
    type: Optional[str] = "percentage"
    discount_value: float
    min_order_value: Optional[float] = 0
    max_discount: Optional[float] = None
    usage_limit: Optional[int] = None
    user_usage_limit: Optional[int] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    is_active: Optional[bool] = True


class CouponUpdate(BaseModel):
    code: Optional[str] = None
    title: Optional[str] = None
    type: Optional[str] = None
    discount_value: Optional[float] = None
    min_order_value: Optional[float] = None
    max_discount: Optional[float] = None
    usage_limit: Optional[int] = None
    user_usage_limit: Optional[int] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    is_active: Optional[bool] = None


@router.get("")
async def list_coupons(
    active_only: bool = False,
    user: UserContext = Depends(require_permission("promotion.read")),
):
    return await _svc.list_coupons(user, active_only=active_only)


@router.get("/{coupon_id}")
async def get_coupon(
    coupon_id: int,
    user: UserContext = Depends(require_permission("promotion.read")),
):
    return await _svc.get_coupon(user, coupon_id)


@router.get("/{coupon_id}/usage")
async def get_coupon_usage(
    coupon_id: int,
    user: UserContext = Depends(require_permission("promotion.read")),
):
    return await _svc.get_coupon_usage(user, coupon_id)


@router.post("", status_code=201)
async def create_coupon(
    body: CouponCreate,
    user: UserContext = Depends(require_permission("promotion.write")),
):
    return await _svc.create_coupon(user, body.model_dump())


@router.patch("/{coupon_id}")
async def update_coupon(
    coupon_id: int,
    body: CouponUpdate,
    user: UserContext = Depends(require_permission("promotion.write")),
):
    return await _svc.update_coupon(user, coupon_id, body.model_dump(exclude_unset=True))


@router.delete("/{coupon_id}")
async def delete_coupon(
    coupon_id: int,
    user: UserContext = Depends(require_permission("promotion.delete")),
):
    return await _svc.delete_coupon(user, coupon_id)
