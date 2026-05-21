"""Razorpay extended endpoints — Customers, QR Codes."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.razorpay import qr_codes as rzp_qr
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
    # Gate: don't issue collection QRs for merchants who haven't finished
    # Route onboarding — captured funds would have no settlement path.
    from app.services.razorpay.route_service import rzp_route_service
    try:
        await rzp_route_service.assert_settlement_ready(
            merchant_id=str(user.restaurant_id) if user.restaurant_id else None,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=409, detail=f"merchant_not_settlement_ready: {exc}",
        )
    return await _svc.create_qr_code(
        name=body.name,
        amount_paise=body.amount_paise,
        description=body.description,
        close_by=body.close_by,
    )


@router.get("/qr-codes/{qr_id}/payments")
async def list_qr_payments(
    qr_id: str,
    user: UserContext = Depends(require_permission("razorpay.qr.read")),
):
    """
    Mirrors Razorpay `GET /v1/payments/qr_codes/{qr_id}/payments`
    (Fetch Payments for a QR Code). Returns the raw Razorpay payload —
    `{"entity": "collection", "count": N, "items": [...]}` — so the
    Flutter side can render the full payment objects without a second
    round-trip per row.
    """
    merchant_id = str(user.restaurant_id) if user.restaurant_id else None
    if not merchant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    try:
        return await rzp_qr.fetch_qr_payments(qr_id, merchant_id=merchant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
