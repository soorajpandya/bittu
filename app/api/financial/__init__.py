"""Financial Infrastructure APIs — /api/financial/*

Audience:    internal callers (workers) for writes; platform staff for reads
Auth:        Service token (write) / Platform JWT (read)
Tenancy:     tenant in payload

This is the *engine* surface. All money movement endpoints live here.
Platform UI calls these; merchant frontends never do.

Phase-0 skeleton from docs/ARCHITECTURE_V2.md.
"""
from fastapi import APIRouter

router = APIRouter(prefix="/financial/v1", tags=["financial"])
