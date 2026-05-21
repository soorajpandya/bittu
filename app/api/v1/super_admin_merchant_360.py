"""
Super-admin merchant-360.

Prefix:   /super-admin/merchants
Gating:   require_platform_admin()

NOTE: The existing `/super-admin/merchants` list / suspend endpoints
live in `super_admin.py`. This module ADDS the consolidated single
merchant deep-dive at `/{restaurant_id}/360`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path

from app.core.auth import UserContext, require_platform_admin
from app.services.super_admin import merchant_360_service

router = APIRouter(
    prefix="/super-admin/merchants", tags=["Super Admin · Merchant 360"],
)


@router.get("/{restaurant_id}/360")
async def merchant_360(
    restaurant_id: str = Path(..., min_length=8, max_length=64),
    _: UserContext = Depends(require_platform_admin()),
):
    """Identity + KYC + Route + wallet + recent activity in one payload."""
    try:
        return await merchant_360_service.merchant_360(restaurant_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
