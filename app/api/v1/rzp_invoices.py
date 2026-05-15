"""
Razorpay Invoices REST API (Phase 9 — hosted invoices).

Prefix ``/razorpay-invoices``. All gateway side-effects funnel through
``rzp_invoice_service`` so idempotency keys and merchant resolution
stay in exactly one place.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.logging import get_logger
from app.services.razorpay.invoice_service import rzp_invoice_service

logger = get_logger(__name__)

router = APIRouter(prefix="/razorpay-invoices", tags=["Razorpay Invoices"])


def _mid(user: UserContext) -> str:
    if not user.restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    return str(user.restaurant_id)


def _bid(user: UserContext) -> Optional[str]:
    bid = getattr(user, "branch_id", None)
    return str(bid) if bid else None


# ── Models ────────────────────────────────────────────────────────────────


class CreateInvoiceIn(BaseModel):
    amount_paise: int = Field(..., ge=100)
    currency: str = Field("INR", min_length=3, max_length=3)
    customer: Optional[dict[str, Any]] = None
    customer_id: Optional[str] = None
    description: Optional[str] = None
    receipt: Optional[str] = None
    line_items: Optional[list[dict[str, Any]]] = None
    notes: Optional[dict[str, Any]] = None
    sms_notify: bool = True
    email_notify: bool = True
    expire_by_epoch: Optional[int] = Field(
        None, description="Unix epoch at which invoice auto-expires",
    )
    internal_order_id: Optional[str] = Field(
        None,
        description="Optional Bittu order UUID to bind this invoice to",
    )


class UpdateInvoiceIn(BaseModel):
    body: dict[str, Any] = Field(
        ..., description="Razorpay-shape patch body (only allowed on draft invoices)",
    )


class NotifyIn(BaseModel):
    medium: str = Field(..., regex="^(sms|email)$")


# ── Invoices ──────────────────────────────────────────────────────────────


@router.get("")
async def list_invoices(
    status: Optional[str] = Query(
        None, regex="^(draft|issued|partially_paid|paid|expired|cancelled)$"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("razorpay.invoices.read")),
):
    return await rzp_invoice_service.list_invoices(
        merchant_id=_mid(user), status=status, limit=limit, offset=offset,
    )


@router.get("/{invoice_id}")
async def get_invoice(
    invoice_id: str,
    user: UserContext = Depends(require_permission("razorpay.invoices.read")),
):
    row = await rzp_invoice_service.get_invoice(
        merchant_id=_mid(user), invoice_id=invoice_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="invoice_not_found")
    return row


@router.post("")
async def create_invoice(
    body: CreateInvoiceIn,
    user: UserContext = Depends(require_permission("razorpay.invoices.write")),
):
    try:
        return await rzp_invoice_service.create_invoice(
            merchant_id=_mid(user),
            branch_id=_bid(user),
            internal_order_id=body.internal_order_id,
            amount_paise=body.amount_paise,
            currency=body.currency,
            customer=body.customer,
            customer_id=body.customer_id,
            description=body.description,
            receipt=body.receipt,
            line_items=body.line_items,
            notes=body.notes,
            sms_notify=body.sms_notify,
            email_notify=body.email_notify,
            expire_by_epoch=body.expire_by_epoch,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{invoice_id}/issue")
async def issue_invoice(
    invoice_id: str,
    user: UserContext = Depends(require_permission("razorpay.invoices.write")),
):
    try:
        return await rzp_invoice_service.issue_invoice(
            merchant_id=_mid(user), invoice_id=invoice_id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="invoice_not_found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")


@router.post("/{invoice_id}/cancel")
async def cancel_invoice(
    invoice_id: str,
    user: UserContext = Depends(require_permission("razorpay.invoices.write")),
):
    try:
        return await rzp_invoice_service.cancel_invoice(
            merchant_id=_mid(user), invoice_id=invoice_id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="invoice_not_found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{invoice_id}/sync")
async def sync_invoice(
    invoice_id: str,
    user: UserContext = Depends(require_permission("razorpay.invoices.read")),
):
    try:
        return await rzp_invoice_service.sync_invoice(
            merchant_id=_mid(user), invoice_id=invoice_id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="invoice_not_found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")


@router.post("/{invoice_id}/notify")
async def notify_invoice(
    invoice_id: str,
    body: NotifyIn,
    user: UserContext = Depends(require_permission("razorpay.invoices.write")),
):
    try:
        return await rzp_invoice_service.notify_invoice(
            merchant_id=_mid(user), invoice_id=invoice_id, medium=body.medium,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="invoice_not_found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/{invoice_id}")
async def update_invoice(
    invoice_id: str,
    body: UpdateInvoiceIn,
    user: UserContext = Depends(require_permission("razorpay.invoices.write")),
):
    try:
        return await rzp_invoice_service.update_invoice(
            merchant_id=_mid(user), invoice_id=invoice_id, body=body.body,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="invoice_not_found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="forbidden")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


__all__ = ["router"]
