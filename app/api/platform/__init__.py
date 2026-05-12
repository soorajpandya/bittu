"""Platform Admin APIs — /api/platform/*

Audience:    Bittu staff (super_admin / finance_admin / recon_admin /
             risk_admin / support_admin)
Auth:        Platform JWT
Tenancy:     global (no tenant scoping)

This package is the Phase-0 skeleton from docs/ARCHITECTURE_V2.md.
Domain handlers will move here from app/api/v1/admin_*.py in later phases.
"""
from fastapi import APIRouter

router = APIRouter(prefix="/platform/v1", tags=["platform"])
