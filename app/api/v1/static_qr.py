"""
Static QR Payment Module — REST API.

Prefix ``/razorpay-static-qr``. Owns the dedicated multi-use Razorpay QR
codes used for "scan & pay any amount" merchant counter QRs. This is a
NET-NEW surface that runs alongside (and never touches) the existing
order-driven QR/checkout flow.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.logging import get_logger
from app.services.razorpay import static_qr_service as svc

logger = get_logger(__name__)

router = APIRouter(prefix="/razorpay-static-qr", tags=["Razorpay Static QR"])


def _mid(user: UserContext) -> str:
    if not user.restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    return str(user.restaurant_id)


# ── Models ────────────────────────────────────────────────────────────────


class CreateStaticQrIn(BaseModel):
    fallback_name: Optional[str] = Field(
        default=None,
        max_length=120,
        description=(
            "Last-resort merchant display name; ignored when the linked "
            "account's legal_business_name or restaurants.name is available."
        ),
    )


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get("")
async def get_static_qr(
    user: UserContext = Depends(require_permission("razorpay.qr.read")),
):
    """Return the merchant's currently active static QR (if any)."""
    row = await svc.get_active_static_qr(_mid(user))
    if not row:
        return {"static_qr": None}
    return {"static_qr": row}


@router.post("")
async def create_static_qr(
    payload: CreateStaticQrIn | None = None,
    user: UserContext = Depends(require_permission("razorpay.qr.write")),
):
    """Create (or replay the existing) static QR for the calling merchant.

    Idempotent — subsequent calls return the same active QR until it is
    explicitly regenerated via ``POST /regenerate``.
    """
    fallback_name = (payload.fallback_name if payload else None)
    row = await svc.create_static_qr(
        merchant_id=_mid(user), fallback_name=fallback_name,
    )
    return {"static_qr": row}


@router.post("/regenerate")
async def regenerate_static_qr(
    payload: CreateStaticQrIn | None = None,
    user: UserContext = Depends(require_permission("razorpay.qr.write")),
):
    """Close the merchant's existing static QR and mint a fresh one."""
    fallback_name = (payload.fallback_name if payload else None)
    row = await svc.regenerate_static_qr(
        merchant_id=_mid(user), fallback_name=fallback_name,
    )
    return {"static_qr": row}


@router.post("/close")
async def close_static_qr(
    user: UserContext = Depends(require_permission("razorpay.qr.write")),
):
    """Close the merchant's active static QR without minting a new one."""
    row = await svc.close_static_qr(_mid(user))
    return {"static_qr": row}


@router.get("/payments")
async def list_static_qr_payments(
    status: Optional[str] = Query(
        default=None, pattern="^(authorized|captured|failed|refunded)$",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: UserContext = Depends(require_permission("razorpay.qr.read")),
):
    """Paginated list of Razorpay-webhook-driven payments for this merchant's
    static QR(s). ``status=failed`` powers the FE *Failed Payments* tab.
    """
    return await svc.list_static_qr_payments(
        merchant_id=_mid(user), status=status, limit=limit, offset=offset,
    )
