"""Subscription & Billing endpoints."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_role
from app.services.subscription_service import SubscriptionService

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])
_svc = SubscriptionService()


@router.get("/status")
async def check_subscription(
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.check_active(user.user_id)


@router.get("/plans")
async def list_plans():
    """List all available subscription plans (public)."""
    return await _svc.get_plans()


@router.post("/verify")
async def verify_subscription(
    user: UserContext = Depends(require_role("owner", "manager", "cashier", "chef", "waiter", "staff")),
):
    """Verify the current user's subscription status."""
    return await _svc.verify_subscription(user)


@router.post("/free-trial")
async def start_free_trial(
    user: UserContext = Depends(require_role("owner")),
):
    """Start a free trial for the current user."""
    return await _svc.start_free_trial(user)


@router.get("")
async def get_subscription(
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.get_subscription(user)
