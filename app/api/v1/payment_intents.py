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

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.core.database import get_service_connection
from app.core.logging import get_logger

router = APIRouter(prefix="/payment-intents", tags=["Payments"])
logger = get_logger(__name__)

# In-process throttle: per internal_order_id → monotonic timestamp of last
# opportunistic Razorpay pull. Bounded; never grows past ~5k entries because
# closed orders stop polling. Process-local is fine — even with 4 workers
# each one will at worst pull once per window.
_LAST_PULLTHROUGH_AT: dict[str, float] = {}
_PULLTHROUGH_MIN_INTERVAL_S = 5.0   # don't hit Razorpay more than 1×/5s/order
_PULLTHROUGH_MIN_AGE_S = 8.0        # wait 8s after order creation before first pull
_FORCE_REFRESH_AFTER_S = 25.0       # tell FE to show "taking longer than usual" UI


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
    # ─ Polling hints for clients (Flutter POS) ─
    # `next_poll_after_ms` — recommended delay before next GET.
    # `should_force_refresh` — true after ~25s of pending state; FE should
    # show a "taking longer than usual / try again" affordance.
    # `seconds_since_created` — wall-clock age of the rzp_orders row.
    # `is_terminal` — true when no further state change is expected
    # (captured / failed / cancelled / refunded). FE should stop polling.
    next_poll_after_ms: int = 2000
    should_force_refresh: bool = False
    seconds_since_created: int = 0
    is_terminal: bool = False


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


_TERMINAL_PAYMENT_STATUSES = {"captured", "failed", "refunded", "cancelled"}
_PENDING_PAYMENT_STATUSES = {None, "initiated", "pending", "created", "authorized"}


async def _read_intent_row(conn, merchant_id, order_id):
    return await conn.fetchrow(
        """
        SELECT
            o.internal_order_id::text       AS internal_order_id,
            o.razorpay_order_id,
            o.amount_paise,
            o.amount_paid_paise,
            o.amount_due_paise,
            o.currency,
            o.status::text                  AS status,
            o.created_at                    AS rzp_order_created_at,
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
        merchant_id,
        order_id,
    )


async def _pull_through_from_razorpay(
    *,
    merchant_id: str,
    internal_order_id: str,
    razorpay_order_id: str,
) -> bool:
    """
    Best-effort: pull the latest payments for this Razorpay order and feed
    any captured/authorized/failed payment into the webhook dispatcher so
    the local mirror catches up. Returns True if at least one event was
    dispatched (meaning the next DB read should show a fresher state).

    Safe to call repeatedly — the dispatcher is idempotent on (event,
    razorpay_payment_id).
    """
    from app.services.razorpay.orders import fetch_order_payments
    from app.services.razorpay.webhook_dispatcher import (
        dispatch_event as rzp_dispatch_event,
    )

    try:
        resp = await fetch_order_payments(
            razorpay_order_id, merchant_id=str(merchant_id),
        )
    except Exception as exc:  # network / 5xx / auth — swallow & try later
        logger.warning(
            "rzp_intent_pullthrough_fetch_failed",
            order_id=internal_order_id,
            razorpay_order_id=razorpay_order_id,
            error=str(exc)[:200],
        )
        return False

    items = resp.get("items") or []
    dispatched = False
    # Process oldest first so authorized→captured ordering is preserved.
    for pay in sorted(items, key=lambda p: p.get("created_at") or 0):
        pay_status = (pay.get("status") or "").lower()
        event_name = {
            "captured": "payment.captured",
            "authorized": "payment.authorized",
            "failed": "payment.failed",
        }.get(pay_status)
        if not event_name:
            continue
        envelope = {
            "event": event_name,
            "account_id": pay.get("account_id"),
            "contains": ["payment"],
            "payload": {"payment": {"entity": pay}},
            "created_at": pay.get("created_at"),
        }
        try:
            await rzp_dispatch_event(
                event=event_name, envelope=envelope, signature=None,
            )
            dispatched = True
        except Exception:
            logger.exception(
                "rzp_intent_pullthrough_dispatch_failed",
                order_id=internal_order_id,
                razorpay_payment_id=pay.get("id"),
                event=event_name,
            )
    return dispatched


def _decorate_intent_hints(out: IntentOut, *, created_at) -> IntentOut:
    """Attach polling hints + terminality based on current state + age."""
    age_s = 0
    if created_at is not None:
        try:
            age_s = max(0, int(time.time() - created_at.timestamp()))
        except Exception:
            age_s = 0
    is_terminal = (out.payment_status or "") in _TERMINAL_PAYMENT_STATUSES
    if is_terminal:
        next_ms = 0
    elif age_s < 10:
        next_ms = 1500
    elif age_s < 30:
        next_ms = 2500
    else:
        next_ms = 5000
    out.next_poll_after_ms = next_ms
    out.should_force_refresh = (not is_terminal) and age_s >= _FORCE_REFRESH_AFTER_S
    out.seconds_since_created = age_s
    out.is_terminal = is_terminal
    return out


@router.get(
    "/{order_id}",
    response_model=IntentOut,
    summary="Read the Razorpay payment intent for an internal order",
)
async def get_intent(
    order_id: str = Path(..., description="Internal order UUID"),
    force_sync: bool = Query(
        False,
        description=(
            "If true, the backend pulls the live order/payment state from "
            "Razorpay before responding. Use sparingly (manual refresh "
            "button) — the endpoint also self-heals automatically every "
            "~5s while the payment is pending."
        ),
    ),
    user: UserContext = Depends(require_permission("razorpay.orders.read")),
):
    async with get_service_connection() as conn:
        row = await _read_intent_row(conn, user.restaurant_id, order_id)

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="payment intent not found for this order",
        )

    # Opportunistic pull-through: if the payment isn't terminal and the
    # order has been alive long enough (or the caller asked for it), ask
    # Razorpay for the truth. This is the safety net for missed webhooks.
    needs_pull = force_sync or (
        (row["payment_status"] in _PENDING_PAYMENT_STATUSES)
    )
    if needs_pull:
        created_at = row["rzp_order_created_at"]
        age_s = 0
        if created_at is not None:
            try:
                age_s = time.time() - created_at.timestamp()
            except Exception:
                age_s = 0.0
        last = _LAST_PULLTHROUGH_AT.get(order_id, 0.0)
        now = time.monotonic()
        old_enough = force_sync or age_s >= _PULLTHROUGH_MIN_AGE_S
        cooled_down = force_sync or (now - last) >= _PULLTHROUGH_MIN_INTERVAL_S
        if old_enough and cooled_down:
            _LAST_PULLTHROUGH_AT[order_id] = now
            dispatched = await _pull_through_from_razorpay(
                merchant_id=str(user.restaurant_id),
                internal_order_id=order_id,
                razorpay_order_id=row["razorpay_order_id"],
            )
            if dispatched:
                # Re-read so the response reflects what we just synced.
                async with get_service_connection() as conn:
                    row = await _read_intent_row(
                        conn, user.restaurant_id, order_id,
                    )
                if row is None:  # extremely unlikely race
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="payment intent not found for this order",
                    )

    out = IntentOut(
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
    return _decorate_intent_hints(out, created_at=row["rzp_order_created_at"])


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


# ── manual cancel (Phase 5) ──────────────────────────────────────────────


class CancelIn(BaseModel):
    reason: Optional[str] = None


@router.post(
    "/{order_id}/cancel",
    response_model=IntentOut,
    summary="Cancel a pending Razorpay payment intent / close its QR",
)
async def cancel_intent(
    body: Optional[CancelIn] = None,
    order_id: str = Path(..., description="Internal order UUID"),
    user: UserContext = Depends(require_permission("razorpay.orders.write")),
):
    """
    Operator-driven cancellation of an *unpaid* payment intent.

    Steps (all idempotent, safe to replay):

      1. Resolve the latest rzp_payment / rzp_qr for the order, scoped to
         the caller's merchant.
      2. Reject (409) if the payment is already authorized / captured /
         refunded — those need the refund flow, not cancel.
      3. Best-effort: call Razorpay `qr_codes/{id}/close` on the linked
         QR if it is still active. Swallow 4xx (already closed) and 5xx
         (Razorpay outage — webhook will reconcile later).
      4. Guarded UPDATE: flip `payments.status` from
         {initiated, pending, failed} → `cancelled`. Never touches a
         row already authorized/captured/refunded.
      5. Flip `orders.status` → `cancelled` only when the order is still
         `pending_payment` (uses the existing order-status flow).
      6. Emit `PAYMENT_CANCELLED` + `ORDER_CANCELLED` so realtime + reports
         drop the row from "pending" lists immediately.
      7. Return the refreshed intent so the FE can re-render in one round
         trip.
    """
    from app.core.events import (
        DomainEvent,
        PAYMENT_CANCELLED,
        ORDER_CANCELLED,
        emit_and_publish,
    )

    reason = (body.reason if body else None) or "merchant_cancelled"

    async with get_service_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                o.internal_order_id::text   AS internal_order_id,
                o.razorpay_order_id         AS razorpay_order_id,
                o.merchant_id::text         AS merchant_id,
                q.qr_id                     AS qr_id,
                q.status::text              AS qr_status,
                p.id::text                  AS payment_id,
                p.status::text              AS payment_status,
                p.amount                    AS amount,
                p.currency                  AS currency,
                p.branch_id::text           AS branch_id,
                ord.status                  AS order_status
            FROM rzp_orders o
            LEFT JOIN rzp_qr_order_links l
                   ON l.rzp_order_uuid = o.id AND l.is_primary = TRUE
            LEFT JOIN rzp_qr_codes q
                   ON q.qr_id = l.qr_id
            LEFT JOIN payments p
                   ON p.order_id = o.internal_order_id
                  AND p.restaurant_id = o.merchant_id
            LEFT JOIN orders ord
                   ON ord.id = o.internal_order_id
            WHERE o.merchant_id       = $1::uuid
              AND o.internal_order_id = $2::uuid
            ORDER BY p.created_at DESC NULLS LAST
            LIMIT 1
            """,
            user.restaurant_id,
            order_id,
        )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="payment intent not found for this merchant",
        )

    payment_status = (row["payment_status"] or "").lower()
    if payment_status in ("authorized", "captured", "completed", "refunded"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"payment is '{payment_status}' — cancel is not allowed; "
                f"use the refund flow instead"
            ),
        )

    # ── (3) Best-effort: close the live QR on Razorpay ──
    qr_id = row["qr_id"]
    qr_status = (row["qr_status"] or "").lower()
    if qr_id and qr_status == "active":
        try:
            from app.services.razorpay import qr_codes as rzp_qr_api
            await rzp_qr_api.close_qr(qr_id, merchant_id=str(user.restaurant_id))
            logger.info("rzp_qr_closed_on_cancel", qr_id=qr_id, order_id=order_id)
        except Exception as exc:
            # Already closed (4xx) or transient (5xx) — webhook will reconcile.
            logger.warning(
                "rzp_qr_close_best_effort_failed",
                qr_id=qr_id, order_id=order_id, error=str(exc),
            )

    # ── (4) Flip internal payment → cancelled, guarded ──
    updated_payment_id: Optional[str] = None
    async with get_service_connection() as conn:
        updated = await conn.fetchrow(
            """
            UPDATE payments
               SET status = 'cancelled',
                   updated_at = NOW()
             WHERE order_id = $1::uuid
               AND status IN ('initiated','pending','failed')
            RETURNING id::text AS payment_id
            """,
            order_id,
        )
    if updated:
        updated_payment_id = updated["payment_id"]

    # ── (5) Flip order → cancelled only if still pending_payment ──
    order_flipped = False
    async with get_service_connection() as conn:
        ord_row = await conn.fetchrow(
            """
            UPDATE orders
               SET status = 'cancelled',
                   updated_at = NOW()
             WHERE id = $1::uuid
               AND restaurant_id = $2::uuid
               AND status IN ('pending_payment','awaiting_payment','pending','Pending','PendingPayment')
            RETURNING id
            """,
            order_id,
            user.restaurant_id,
        )
        order_flipped = ord_row is not None

    # ── (6) Emit realtime events (best-effort) ──
    try:
        await emit_and_publish(DomainEvent(
            event_type=PAYMENT_CANCELLED,
            payload={
                "order_id":   order_id,
                "payment_id": updated_payment_id or row["payment_id"],
                "qr_id":      qr_id,
                "amount":     float(row["amount"] or 0),
                "currency":   row["currency"] or "INR",
                "reason":     reason,
            },
            restaurant_id=row["merchant_id"],
            branch_id=row["branch_id"],
            user_id=getattr(user, "user_id", None),
        ))
        if order_flipped:
            await emit_and_publish(DomainEvent(
                event_type=ORDER_CANCELLED,
                payload={
                    "order_id": order_id,
                    "reason":   reason,
                    "source":   "payment_intent_cancel",
                },
                restaurant_id=row["merchant_id"],
                branch_id=row["branch_id"],
                user_id=getattr(user, "user_id", None),
            ))
    except Exception:
        logger.exception("rzp_cancel_emit_failed", order_id=order_id)

    logger.info(
        "rzp_payment_intent_cancelled",
        order_id=order_id,
        payment_id=updated_payment_id,
        qr_id=qr_id,
        order_flipped=order_flipped,
        reason=reason,
    )

    return await get_intent(order_id=order_id, user=user)
