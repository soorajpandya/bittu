"""
Razorpay payment-intent + QR status APIs (Phase 2, read-only for clients).

These endpoints surface the Razorpay-side state of a checkout — POS uses
them to render the QR, poll for payment confirmation, and refresh a stale
intent if Razorpay's response was lost in flight.

All routes are tenant-scoped against `merchant_id = user.restaurant_id` —
even though the underlying tables run with RLS disabled (gateway tables
are mostly cross-merchant by design), the WHERE clause locks each query
to the caller's tenant.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.core.database import get_service_connection
from app.core.logging import get_logger

router = APIRouter(prefix="/payment-intents", tags=["Payments"])
logger = get_logger(__name__)


# ── response models ──────────────────────────────────────────────────────


class IntentOut(BaseModel):
    internal_order_id: str
    razorpay_order_id: str
    amount_paise: int
    amount_paid_paise: int
    amount_due_paise: int
    currency: str
    status: str
    qr_id: Optional[str] = None
    qr_image_url: Optional[str] = None
    qr_image_content: Optional[str] = None
    qr_status: Optional[str] = None
    qr_close_by: Optional[str] = None
    payment_status: Optional[str] = None
    razorpay_payment_id: Optional[str] = None


class QrOut(BaseModel):
    qr_id: str
    status: str
    amount_paise: Optional[int] = None
    image_url: Optional[str] = None
    image_content: Optional[str] = None
    close_by: Optional[str] = None
    closed_at: Optional[str] = None
    payments_amount_received_paise: int
    payments_count_received: int


# ── endpoints ────────────────────────────────────────────────────────────


@router.get(
    "/{order_id}",
    response_model=IntentOut,
    summary="Read the Razorpay payment intent for an internal order",
)
async def get_intent(
    order_id: str = Path(..., description="Internal order UUID"),
    user: UserContext = Depends(require_permission("razorpay.orders.read")),
):
    async with get_service_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                o.internal_order_id::text       AS internal_order_id,
                o.razorpay_order_id,
                o.amount_paise,
                o.amount_paid_paise,
                o.amount_due_paise,
                o.currency,
                o.status::text                  AS status,
                q.qr_id,
                q.image_url,
                q.image_content,
                q.status::text                  AS qr_status,
                q.close_by,
                p.status::text                  AS payment_status,
                p.razorpay_payment_id
            FROM rzp_orders o
            LEFT JOIN rzp_qr_order_links l
                   ON l.rzp_order_uuid = o.id
                  AND l.is_primary = TRUE
            LEFT JOIN rzp_qr_codes q
                   ON q.qr_id = l.qr_id
            LEFT JOIN payments p
                   ON p.order_id = o.internal_order_id
                  AND p.restaurant_id = o.merchant_id
            WHERE o.merchant_id = $1::uuid
              AND o.internal_order_id = $2::uuid
            ORDER BY o.created_at DESC
            LIMIT 1
            """,
            user.restaurant_id,
            order_id,
        )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="payment intent not found for this order",
        )

    return IntentOut(
        internal_order_id=row["internal_order_id"],
        razorpay_order_id=row["razorpay_order_id"],
        amount_paise=int(row["amount_paise"]),
        amount_paid_paise=int(row["amount_paid_paise"]),
        amount_due_paise=int(row["amount_due_paise"]),
        currency=row["currency"],
        status=row["status"],
        qr_id=row["qr_id"],
        qr_image_url=row["image_url"],
        qr_image_content=row["image_content"],
        qr_status=row["qr_status"],
        qr_close_by=row["close_by"].isoformat() if row["close_by"] else None,
        payment_status=row["payment_status"],
        razorpay_payment_id=row["razorpay_payment_id"],
    )


@router.get(
    "/{order_id}/qr",
    response_model=QrOut,
    summary="Read the active QR for an internal order",
)
async def get_intent_qr(
    order_id: str = Path(..., description="Internal order UUID"),
    user: UserContext = Depends(require_permission("razorpay.qr.read")),
):
    async with get_service_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                q.qr_id,
                q.status::text                  AS status,
                q.amount_paise,
                q.image_url,
                q.image_content,
                q.close_by,
                q.closed_at,
                q.payments_amount_received_paise,
                q.payments_count_received
            FROM rzp_qr_order_links l
            JOIN rzp_qr_codes q ON q.qr_id = l.qr_id
            WHERE l.merchant_id = $1::uuid
              AND l.internal_order_id = $2::uuid
              AND l.is_primary = TRUE
            ORDER BY l.created_at DESC
            LIMIT 1
            """,
            user.restaurant_id,
            order_id,
        )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no QR registered for this order",
        )

    return QrOut(
        qr_id=row["qr_id"],
        status=row["status"],
        amount_paise=int(row["amount_paise"]) if row["amount_paise"] is not None else None,
        image_url=row["image_url"],
        image_content=row["image_content"],
        close_by=row["close_by"].isoformat() if row["close_by"] else None,
        closed_at=row["closed_at"].isoformat() if row["closed_at"] else None,
        payments_amount_received_paise=int(row["payments_amount_received_paise"] or 0),
        payments_count_received=int(row["payments_count_received"] or 0),
    )


@router.post(
    "/{order_id}/refresh",
    response_model=IntentOut,
    summary="Force-create or replay a Razorpay intent for an internal order",
)
async def refresh_intent(
    order_id: str = Path(..., description="Internal order UUID"),
    user: UserContext = Depends(require_permission("razorpay.orders.write")),
):
    """
    Idempotent backstop: if checkout's gateway call dropped on the floor,
    the POS can call this to retry. If the intent already exists it's
    replayed verbatim (no second Razorpay order is created).
    """
    from decimal import Decimal

    async with get_service_connection() as conn:
        order = await conn.fetchrow(
            """
            SELECT o.id::text               AS internal_order_id,
                   o.restaurant_id::text    AS merchant_id,
                   o.branch_id::text        AS branch_id,
                   COALESCE(o.metadata->>'order_number', o.id::text) AS order_number,
                   o.total_amount,
                   p.id::text               AS payment_id,
                   p.status::text           AS payment_status,
                   p.method::text           AS method
            FROM orders o
            LEFT JOIN payments p ON p.order_id = o.id
            WHERE o.id = $1::uuid
              AND o.restaurant_id = $2::uuid
            ORDER BY p.created_at DESC NULLS LAST
            LIMIT 1
            """,
            order_id,
            user.restaurant_id,
        )

    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="order not found",
        )
    if not order["payment_id"] or order["method"] != "online":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="order is not an online-payment order",
        )
    if order["payment_status"] not in ("pending", "initiated", "failed"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"payment already {order['payment_status']}",
        )

    from app.services.razorpay.payment_intent import create_intent_for_order
    # Enrich notes with customer + creator + owner for dashboard/webhook visibility.
    async with get_service_connection() as conn:
        enrich = await conn.fetchrow(
            """
            SELECT o.user_id              AS created_by_user_id,
                   o.customer_id          AS customer_id,
                   c.name                 AS customer_name,
                   c.phone_number         AS customer_phone
              FROM orders o
              LEFT JOIN customers c ON c.id = o.customer_id
             WHERE o.id = $1::uuid
            """,
            order_id,
        )
    try:
        await create_intent_for_order(
            merchant_id=user.restaurant_id,
            branch_id=order["branch_id"],
            internal_order_id=order["internal_order_id"],
            payment_id=order["payment_id"],
            amount=Decimal(str(order["total_amount"])),
            receipt=order["order_number"],
            customer_name=enrich["customer_name"] if enrich else None,
            customer_phone=enrich["customer_phone"] if enrich else None,
            customer_id=str(enrich["customer_id"]) if enrich and enrich["customer_id"] is not None else None,
            created_by_user_id=enrich["created_by_user_id"] if enrich else None,
            owner_user_id=getattr(user, "owner_id", None) or user.user_id,
            create_qr=True,
        )
    except Exception as exc:
        logger.error(
            "rzp_intent_refresh_failed",
            order_id=order_id, error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="razorpay intent creation failed",
        )

    # Re-read via the same path the GET uses so the client sees a single shape.
    return await get_intent(order_id=order_id, user=user)


# ── manual capture (Phase 4) ─────────────────────────────────────────────


class CaptureIn(BaseModel):
    amount_paise: Optional[int] = None  # default = full authorized amount
    currency: str = "INR"


@router.post(
    "/{order_id}/capture",
    response_model=IntentOut,
    summary="Manually capture an authorized Razorpay payment for an order",
)
async def capture_intent(
    body: CaptureIn,
    order_id: str = Path(..., description="Internal order UUID"),
    user: UserContext = Depends(require_permission("razorpay.payments.capture")),
):
    """
    Idempotent backstop for manual capture (auto-capture disabled, or the
    auto-capture webhook never fired). Driven by the merchant operator —
    NOT by Razorpay. Steps:

      1. Resolve the latest authorized rzp_payment for this internal order.
      2. If already captured → no-op (returns the current intent).
      3. Call Razorpay payments.capture with idempotency key
         ``rzp_capture:{merchant_id}:{rzp_payment_id}``.
      4. Wrap the response into a synthetic webhook envelope and feed it to
         ``dispatch_event(event="payment.captured")`` so the full money
         pipeline (rzp_payments UPSERT, payments→completed, ledger CREDIT,
         escrow HOLD, PAYMENT_COMPLETED event) runs exactly once.
    """
    async with get_service_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                p.razorpay_payment_id,
                p.razorpay_order_id,
                p.status::text          AS status,
                p.amount_paise,
                p.currency,
                p.merchant_id::text     AS merchant_id
            FROM rzp_payments p
            JOIN rzp_orders o
              ON o.razorpay_order_id = p.razorpay_order_id
             AND o.merchant_id       = p.merchant_id
            WHERE o.merchant_id       = $1::uuid
              AND o.internal_order_id = $2::uuid
            ORDER BY p.created_at DESC
            LIMIT 1
            """,
            user.restaurant_id,
            order_id,
        )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no Razorpay payment found for this order",
        )

    rzp_payment_id = row["razorpay_payment_id"]
    cur_status = row["status"]

    if cur_status == "captured":
        # Already captured — nothing to do, return the current intent.
        return await get_intent(order_id=order_id, user=user)

    if cur_status not in ("authorized",):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"payment is in status '{cur_status}'; only 'authorized' may be captured",
        )

    amount_paise = body.amount_paise or int(row["amount_paise"] or 0)
    if amount_paise <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="amount_paise must be > 0",
        )

    from app.services.razorpay import payments as rzp_payments_api
    from app.services.razorpay.webhook_dispatcher import dispatch_event as rzp_dispatch_event

    idem_key = f"rzp_capture:{user.restaurant_id}:{rzp_payment_id}"
    try:
        rzp_resp = await rzp_payments_api.capture_payment(
            rzp_payment_id,
            amount_paise=amount_paise,
            currency=body.currency or row["currency"] or "INR",
            merchant_id=str(user.restaurant_id),
            idempotency_key=idem_key,
        )
    except Exception as exc:
        logger.error(
            "rzp_manual_capture_failed",
            order_id=order_id,
            payment_id=rzp_payment_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"razorpay capture failed: {exc!s}",
        )

    # Drive the full captured pipeline through the dispatcher (idempotent).
    synthetic_envelope = {
        "event": "payment.captured",
        "account_id": rzp_resp.get("account_id"),
        "contains": ["payment"],
        "payload": {"payment": {"entity": rzp_resp}},
        "created_at": rzp_resp.get("created_at"),
    }
    try:
        await rzp_dispatch_event(
            event="payment.captured",
            envelope=synthetic_envelope,
            signature=None,
        )
    except Exception as exc:
        # The gateway already captured — surface a 207-style warning via 200
        # body and let the eventual real webhook reconcile.
        logger.exception(
            "rzp_capture_post_pipeline_failed",
            order_id=order_id,
            payment_id=rzp_payment_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"capture succeeded but post-processing failed: {exc!s}",
        )

    return await get_intent(order_id=order_id, user=user)
