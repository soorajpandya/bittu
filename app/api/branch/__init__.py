"""Branch / Staff APIs — /api/branch/*

Audience:    cashier / waiter / kitchen_staff / inventory_staff
Auth:        Merchant JWT + branch claim
Tenancy:     tenant + branch-scoped

Phase-0 skeleton from docs/ARCHITECTURE_V2.md.
"""
from fastapi import APIRouter

router = APIRouter(prefix="/branch/v1", tags=["branch"])
