"""Razorpay extended endpoints — Customers, Plans, Subscriptions, QR Codes."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.razorpay_extended_service import RazorpayExtendedService

router = APIRouter(prefix="/razorpay", tags=["Razorpay Extended"])
_svc = RazorpayExtendedService()


# ── Request models ──

class CreateCustomerIn(BaseModel):
    name: str
    email: str
    contact: str


class CreatePlanIn(BaseModel):
    plan_name: str
    amount_paise: int
    period: str = "monthly"
    interval: int = 1
    description: str = ""


class CreateSubscriptionIn(BaseModel):
    plan_id: str
    total_count: int
    user_id: str = ""
    plan_name: str = ""
    user_email: str = ""


class CreateQRIn(BaseModel):
    name: str
    amount_paise: int
    description: str = ""
    close_by: int | None = None


# ── Endpoints ──

@router.post("/customers")
async def create_customer(
    body: CreateCustomerIn,
    user: UserContext = Depends(require_permission("payments.create")),
):
    return await _svc.create_customer(body.name, body.email, body.contact)


@router.post("/plans")
async def create_plan(
    body: CreatePlanIn,
    user: UserContext = Depends(require_permission("subscriptions.manage")),
):
    return await _svc.create_plan(
        plan_name=body.plan_name,
        amount_paise=body.amount_paise,
        period=body.period,
        interval=body.interval,
        description=body.description,
    )


@router.get("/plans/{plan_id}")
async def get_plan(
    plan_id: str,
    user: UserContext = Depends(require_permission("subscriptions.manage")),
):
    return await _svc.get_plan(plan_id)


@router.post("/subscriptions")
async def create_subscription(
    body: CreateSubscriptionIn,
    user: UserContext = Depends(require_permission("subscriptions.manage")),
):
    return await _svc.create_subscription(
        plan_id=body.plan_id,
        total_count=body.total_count,
        user_id=body.user_id or user.user_id,
        plan_name=body.plan_name,
        user_email=body.user_email,
    )


@router.get("/subscriptions/{subscription_id}")
async def fetch_subscription(
    subscription_id: str,
    user: UserContext = Depends(require_permission("subscriptions.manage")),
):
    return await _svc.fetch_subscription(subscription_id)


@router.post("/qr-codes")
async def create_qr_code(
    body: CreateQRIn,
    user: UserContext = Depends(require_permission("payments.create")),
):
    return await _svc.create_qr_code(
        name=body.name,
        amount_paise=body.amount_paise,
        description=body.description,
        close_by=body.close_by,
    )
