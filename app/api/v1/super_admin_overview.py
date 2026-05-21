"""
Super-admin platform overview — single KPI dashboard.

Prefix:   /super-admin/overview
Gating:   require_platform_admin()
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.auth import UserContext, require_platform_admin
from app.services.super_admin import overview_service

router = APIRouter(prefix="/super-admin/overview", tags=["Super Admin · Overview"])


@router.get("")
async def get_platform_overview(
    _: UserContext = Depends(require_platform_admin()),
):
    """Single payload of platform-wide KPIs for the cockpit landing page."""
    return await overview_service.platform_overview()
