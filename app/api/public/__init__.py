"""Public APIs — /api/public/*

Audience:    diners (QR), gateway webhooks, callbacks
Auth:        none / signed callback token
Tenancy:     none

Phase-0 skeleton from docs/ARCHITECTURE_V2.md.
"""
from fastapi import APIRouter

router = APIRouter(prefix="/public/v1", tags=["public"])
