"""Subscription & Billing endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission, get_current_user
from app.services.subscription_service import SubscriptionService

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])
_svc = SubscriptionService()


# ── Request Schemas ──

class SubscribeIn(BaseModel):
    plan_slug: str  # starter | growth | pro


class UpgradeIn(BaseModel):
    new_plan_slug: str


class DowngradeIn(BaseModel):
    new_plan_slug: str


class PurchaseAddonIn(BaseModel):
    addon_slug: str
    quantity: int = 1
    shipping_address: Optional[dict] = None


class AdminUpdatePlanIn(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    monthly_price: Optional[float] = None
    description: Optional[str] = None
    features: Optional[list] = None
    limits: Optional[list] = None
    not_included: Optional[list] = None
    highlight: Optional[bool] = None
    highlight_label: Optional[str] = None
    cta_text: Optional[str] = None
    discount_label: Optional[str] = None
    razorpay_plan_id: Optional[str] = None
    is_active: Optional[bool] = None


# ── Public Endpoints ──

@router.get("/plans")
async def list_plans():
    """List all available subscription plans (public)."""
    return await _svc.get_plans()


@router.get("/addons")
async def list_addons():
    """List all available add-on products (public)."""
    return await _svc.list_addons()


# ── User Endpoints ──

@router.get("/status")
async def check_subscription(
    user: UserContext = Depends(require_permission("subscription.read")),
):
    """Check if user has an active subscription."""
    return await _svc.check_active(user.user_id)


@router.get("")
async def get_subscription(
    user: UserContext = Depends(require_permission("subscription.read")),
):
    """Get full subscription details."""
    return await _svc.get_subscription(user)


@router.post("/verify")
async def verify_subscription(
    user: UserContext = Depends(get_current_user),
):
    """Verify subscription status with plan details."""
    return await _svc.verify_subscription(user)


@router.post("/free-trial")
async def start_free_trial(
    user: UserContext = Depends(require_permission("subscription.write")),
):
    """Start a 14-day free trial (one per user)."""
    return await _svc.start_free_trial(user)


@router.post("/subscribe")
async def subscribe(
    body: SubscribeIn,
    user: UserContext = Depends(require_permission("subscription.write")),
):
    """
    Create a subscription. Returns Razorpay subscription details
    including short_url for checkout.
    """
    return await _svc.subscribe(user, body.plan_slug)


@router.post("/cancel")
async def cancel_subscription(
    user: UserContext = Depends(require_permission("subscription.write")),
):
    """
    Cancel subscription. Access continues until current billing period ends.
    Trials are cancelled immediately.
    """
    return await _svc.cancel_subscription(user)


@router.post("/upgrade")
async def upgrade_plan(
    body: UpgradeIn,
    user: UserContext = Depends(require_permission("subscription.write")),
):
    """
    Upgrade to a higher plan. Takes effect immediately.
    Returns Razorpay checkout link for the new plan.
    """
    return await _svc.upgrade_plan(user, body.new_plan_slug)


@router.post("/downgrade")
async def downgrade_plan(
    body: DowngradeIn,
    user: UserContext = Depends(require_permission("subscription.write")),
):
    """
    Schedule a downgrade. Takes effect at next billing cycle.
    """
    return await _svc.downgrade_plan(user, body.new_plan_slug)


@router.post("/addons/purchase")
async def purchase_addon(
    body: PurchaseAddonIn,
    user: UserContext = Depends(require_permission("subscription.write")),
):
    """
    Purchase an add-on (e.g., printer). Returns Razorpay order
    for one-time payment checkout.
    """
    return await _svc.purchase_addon(
        user, body.addon_slug, body.quantity, body.shipping_address
    )


# ── Admin Endpoints ──

@router.get("/admin/list")
async def admin_list_subscriptions(
    status: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("subscription.admin")),
):
    """Admin: View all subscriptions, optionally filtered by status."""
    return await _svc.admin_list_subscriptions(status, limit, offset)


@router.patch("/admin/plans/{plan_id}")
async def admin_update_plan(
    plan_id: int,
    body: AdminUpdatePlanIn,
    user: UserContext = Depends(require_permission("subscription.admin")),
):
    """Admin: Update plan pricing, features, or Razorpay plan ID."""
    return await _svc.admin_update_plan(plan_id, body.model_dump(exclude_unset=True))
