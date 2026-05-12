"""Internal System APIs — /api/internal/*

Audience:    workers, schedulers, M2M
Auth:        HMAC service token + IP allowlist
Tenancy:     global (tenant in payload)

Phase-0 skeleton from docs/ARCHITECTURE_V2.md.
"""
from fastapi import APIRouter

from app.dependencies.service import require_service_token

router = APIRouter(
    prefix="/internal/v1",
    tags=["internal"],
    dependencies=[require_service_token()],
)
