"""Merchant APIs — /api/merchant/*

Audience:    Restaurant owners / merchant_admin / branch_manager
Auth:        Merchant JWT
Tenancy:     tenant-scoped (merchant_id from JWT)

Phase-0 skeleton from docs/ARCHITECTURE_V2.md.
"""
from fastapi import APIRouter

router = APIRouter(prefix="/merchant/v1", tags=["merchant"])
