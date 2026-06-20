"""
Onboarding endpoints — plan selection + Razorpay SaaS-subscription gate.

Flow
----
1. ``GET  /onboarding/plans``                 — plan catalog (prices ex-GST).
2. ``PUT  /onboarding/plan``                  — persist the chosen plan.
3. ``POST /onboarding/subscription``          — create/reuse a Razorpay
   subscription for the recurring "Software" plans (starter/business). For
   the ₹0 integrated-payments plans this returns ``required: false``.
4. ``POST /onboarding/subscription/verify``   — verify the Checkout callback
   and unlock the next steps.
5. ``GET  /onboarding/state``                 — single source of truth the FE
   reads on session restore to route the wizard (plan / subscription / KYC).

The subscription payment is the gate: ``can_proceed_to_settings`` is only
true once the plan is chosen AND (the plan needs no subscription OR the
subscription is authenticated/active).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.auth import UserContext, get_current_user
from app.core.logging import get_logger
from app.services.subscription_service import get_plan_catalog, subscription_service

router = APIRouter(prefix="/onboarding", tags=["Onboarding"])
logger = get_logger(__name__)


class SetPlanIn(BaseModel):
    plan: str = Field(..., description="starter | business | growth | enterprise")


class VerifySubscriptionIn(BaseModel):
    razorpay_payment_id: str = Field(..., min_length=4)
    razorpay_subscription_id: str = Field(..., min_length=4)
    razorpay_signature: str = Field(..., min_length=16)


@router.get("/plans")
async def list_plans(user: UserContext = Depends(get_current_user)):
    """Server-authoritative plan catalog. All prices EXCLUDE GST."""
    return get_plan_catalog()


@router.get("/state")
async def onboarding_state(user: UserContext = Depends(get_current_user)):
    """Current onboarding state — the FE routes the wizard off this."""
    return await subscription_service.get_onboarding_state(user)


@router.put("/plan")
async def set_plan(body: SetPlanIn, user: UserContext = Depends(get_current_user)):
    """Persist the merchant's selected plan and return the new state."""
    return await subscription_service.set_plan(user, body.plan)


@router.get("/subscription")
async def get_subscription(user: UserContext = Depends(get_current_user)):
    """Return the merchant's current subscription state (from onboarding state)."""
    state = await subscription_service.get_onboarding_state(user)
    return state["subscription"]


@router.post("/subscription")
async def create_subscription(user: UserContext = Depends(get_current_user)):
    """Create (or reuse) the Razorpay subscription for the selected plan.

    Returns ``required: false`` for plans with no upfront subscription.
    For software plans returns the ``razorpay_subscription_id``, ``short_url``
    and ``key_id`` the FE needs to open Razorpay Checkout.
    """
    return await subscription_service.create_or_get_subscription(user)


@router.post("/subscription/verify")
async def verify_subscription(
    body: VerifySubscriptionIn, user: UserContext = Depends(get_current_user)
):
    """Verify the Razorpay Checkout subscription callback and unlock next steps."""
    return await subscription_service.verify_subscription_payment(
        user,
        razorpay_payment_id=body.razorpay_payment_id,
        razorpay_subscription_id=body.razorpay_subscription_id,
        razorpay_signature=body.razorpay_signature,
    )
