"""Razorpay extended endpoints — Customers, QR Codes."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.razorpay_extended_service import RazorpayExtendedService

router = APIRouter(prefix="/razorpay", tags=["Payments"])
_svc = RazorpayExtendedService()


# ── Request models ──

class CreateCustomerIn(BaseModel):
    name: str
    email: str
    contact: str


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
