"""Payment endpoints."""
from decimal import Decimal
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status as http_status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.services.payment_service import PaymentService
from app.services.elevenlabs_service import ElevenLabsService
from app.services.activity_log_service import log_activity

from app.core.database import get_connection, get_service_connection
from app.core.logging import get_logger

router = APIRouter(prefix="/payments", tags=["Payments"])
_svc = PaymentService()
_voice_svc = ElevenLabsService()
logger = get_logger(__name__)


# Canonical payment_method enum values (matches payment_method PG enum).
_PM_ALIASES = {
    "cash": "cash", "counter": "cash", "cod": "cash",
    "upi": "upi", "qr_pay": "upi", "qr": "upi", "qr_code": "upi",
    "card": "card", "swipe": "card", "credit": "card", "debit": "card",
    "wallet": "wallet",
    "online": "online", "razorpay": "online", "gateway": "online", "netbanking": "online",
}


class InitiatePaymentIn(BaseModel):
    """
    Frontend-compatible initiate payload.

    Accepts both legacy (`payment_mode`, `amount` in rupees) and Phase-2
    frontend (`payment_mode='online'`, `amount` in paise, plus customer hints).
    Extra fields are ignored so the client schema can evolve independently.
    """
    model_config = {"extra": "ignore"}

    order_id: str
    payment_mode: str = Field(description="cash | upi | card | wallet | online (aliases accepted)")
    amount: float = Field(description="Amount in rupees OR paise — server auto-detects against order total")
    currency: str = "INR"
    tip: float = 0
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    # Customer device GPS — used to enforce per-branch geofence
    # (see app.core.geofence). Optional; when omitted the check is skipped.
    customer_lat: Optional[float] = None
    customer_lng: Optional[float] = None


class VerifyPaymentIn(BaseModel):
    order_id: str
    razorpay_payment_id: str
    razorpay_order_id: str
    razorpay_signature: str


class RefundIn(BaseModel):
    payment_id: str
    amount: Optional[float] = None
    reason: Optional[str] = None


class RecordPaymentIn(BaseModel):
    order_id: str
    method: str = "cash"  # cash | upi | card | wallet | online
    amount: Optional[float] = None


class PaymentVoiceIn(BaseModel):
    amount: float
    language: str = "en"


@router.get("")
async def list_payments(
    order_by: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("payment.create")),
):
    """List payments for the current user's restaurant."""
    try:
        owner_id = user.owner_id if user.is_branch_user else user.user_id
        order_col = "created_at"
        order_dir = "DESC"
        if order_by:
            parts = order_by.split(":")
            allowed = {"created_at", "amount", "status"}
            if parts[0] in allowed:
                order_col = parts[0]
            if len(parts) > 1 and parts[1].lower() == "asc":
                order_dir = "ASC"
        async with get_connection() as conn:
            if user.is_branch_user and user.branch_id:
                rows = await conn.fetch(
                    f"""
                    SELECT p.* FROM payments p
                    JOIN orders o ON o.id = p.order_id
                    WHERE o.user_id = $1 AND o.branch_id = $2
                    ORDER BY p.{order_col} {order_dir}
                    LIMIT $3 OFFSET $4
                    """,
                    owner_id, user.branch_id, limit, offset,
                )
            else:
                rows = await conn.fetch(
                    f"""
                    SELECT p.* FROM payments p
                    JOIN orders o ON o.id = p.order_id
                    WHERE o.user_id = $1
                    ORDER BY p.{order_col} {order_dir}
                    LIMIT $2 OFFSET $3
                    """,
                    owner_id, limit, offset,
                )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("list_payments_failed", error=str(e), user_id=user.user_id)
        return []


@router.post("")
async def record_payment(
    body: RecordPaymentIn,
    user: UserContext = Depends(require_permission("payment.create")),
):
    """Record a payment for an order (called from POS save-and-print)."""
    result = await _svc.initiate_payment(
        user=user,
        order_id=body.order_id,
        method=body.method,
        amount=body.amount,
    )
    await log_activity(
        user_id=user.user_id,
        branch_id=user.branch_id,
        action="payment.created",
        entity_type="payment",
        entity_id=result.get("payment_id") if isinstance(result, dict) else None,
        metadata={"order_id": body.order_id, "method": body.method, "amount": body.amount},
    )
    return result


@router.post("/voice", response_class=Response)
async def payment_voice(
    body: PaymentVoiceIn,
    user: UserContext = Depends(require_permission("voice.use")),
):
    """Play a voice notification for a payment amount."""
    audio = await _voice_svc.payment_voice_notification(
        amount=body.amount,
        language=body.language,
    )
    return Response(content=audio, media_type="audio/mpeg")


@router.post("/initiate")
async def initiate_payment(
    body: InitiatePaymentIn,
    user: UserContext = Depends(require_permission("payment.create")),
):
    """
    Initiate a payment for an order.

    - For ``online`` (alias: razorpay/gateway/netbanking): returns the Razorpay
      intent (razorpay_order_id + QR fields). Idempotent — replays the intent
      that ``/orders/checkout`` already created instead of erroring on the
      "pending payment exists" guard.
    - For ``cash | upi | card | wallet``: marks the payment completed (or
      pending for online) via the legacy ``PaymentService.initiate_payment``
      pathway.

    Accepts ``amount`` in either rupees or paise — auto-detected against the
    order total. Frontend sending paise (Phase-2 contract) works as-is.
    """
    method = _PM_ALIASES.get(str(body.payment_mode).strip().lower())
    if method is None:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported payment_mode: {body.payment_mode!r}",
        )

    # Geo-fence: enforce only when the merchant opted in on this branch.
    # No-op when branch/lat/lng are unset.
    from app.core.geofence import assert_within_geofence
    await assert_within_geofence(
        merchant_id=str(user.restaurant_id),
        branch_id=str(user.branch_id) if user.branch_id else None,
        customer_lat=body.customer_lat,
        customer_lng=body.customer_lng,
    )

    # ── Resolve order total + auto-detect rupees vs paise ──
    # Also pull customer + creator info so we can tag the Razorpay order/QR
    # `notes` (visible in dashboard + webhooks) with WHO is paying and which
    # staff member generated it. Body-supplied customer_name/phone override
    # the DB row when present (walk-in customers).
    async with get_connection() as conn:
        order_row = await conn.fetchrow(
            """
            SELECT o.id::text            AS id,
                   o.restaurant_id::text AS merchant_id,
                   o.branch_id::text     AS branch_id,
                   o.user_id             AS created_by_user_id,
                   o.customer_id         AS customer_id,
                   c.name                AS customer_name,
                   c.phone_number        AS customer_phone,
                   o.total_amount,
                   COALESCE(o.metadata->>'order_number', LEFT(o.id::text, 8)) AS order_number
            FROM orders o
            LEFT JOIN customers c ON c.id = o.customer_id
            WHERE o.id = $1::uuid AND o.restaurant_id = $2::uuid
            """,
            body.order_id, user.restaurant_id,
        )
    if order_row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="order not found",
        )
    order_total = Decimal(str(order_row["total_amount"]))

    # If client sent paise (int much larger than rupee total), divide by 100.
    amt_dec = Decimal(str(body.amount))
    if amt_dec > order_total * Decimal("5"):
        amt_dec = (amt_dec / Decimal("100")).quantize(Decimal("0.01"))

    if amt_dec <= 0 or amt_dec > order_total:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"amount {amt_dec} invalid for order total {order_total}",
        )

    # ────────────────────────────────────────────────────────────────────
    # ONLINE — replay the intent created by /orders/checkout (idempotent).
    # ────────────────────────────────────────────────────────────────────
    if method == "online":
        async with get_service_connection() as conn:
            existing = await conn.fetchrow(
                """
                SELECT p.id::text                AS payment_id,
                       p.status::text            AS payment_status,
                       p.method::text            AS method,
                       p.amount,
                       p.currency,
                       p.razorpay_order_id,
                       p.razorpay_payment_id,
                       o.razorpay_order_id       AS rzp_order_id,
                       o.amount_paise,
                       o.amount_paid_paise,
                       o.amount_due_paise,
                       o.status::text            AS rzp_order_status,
                       q.qr_id,
                       q.image_url               AS qr_image_url,
                       q.image_content           AS qr_image_content,
                       q.close_by                AS qr_close_by,
                       q.status::text            AS qr_status
                FROM payments p
                LEFT JOIN rzp_orders o
                       ON o.internal_order_id = p.order_id
                      AND o.merchant_id       = p.restaurant_id
                LEFT JOIN rzp_qr_order_links l
                       ON l.rzp_order_uuid = o.id
                      AND l.is_primary = TRUE
                LEFT JOIN rzp_qr_codes q
                       ON q.qr_id = l.qr_id
                WHERE p.order_id     = $1::uuid
                  AND p.restaurant_id = $2::uuid
                  AND p.method        = 'online'
                ORDER BY p.created_at DESC
                LIMIT 1
                """,
                body.order_id, user.restaurant_id,
            )

        # Create intent if missing OR replay existing (create_intent_for_order
        # is itself idempotent and returns the existing intent on replay).
        from app.services.razorpay.payment_intent import create_intent_for_order
        from app.core.tenant import tenant_insert_fields
        import uuid as _uuid

        payment_id: Optional[str]
        payment_status_value: str

        if existing is None:
            # No payment row yet — checkout didn't run or chose a different
            # method. Insert a pending payment row, then create the intent.
            tenant = tenant_insert_fields(user)
            payment_id = str(_uuid.uuid4())
            payment_status_value = "pending"
            async with get_connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO payments (
                        id, order_id, restaurant_id, user_id, branch_id,
                        method, status, amount, currency
                    ) VALUES (
                        $1::uuid, $2::uuid, $3::uuid, $4, $5,
                        'online'::payment_method, 'pending'::payment_status, $6, $7
                    )
                    """,
                    payment_id, body.order_id, user.restaurant_id,
                    tenant["user_id"], tenant.get("branch_id"),
                    float(amt_dec), body.currency,
                )
        else:
            payment_id = existing["payment_id"]
            payment_status_value = existing["payment_status"]
            # Terminal — return final state, frontend should stop polling.
            if payment_status_value in ("completed", "failed", "refunded"):
                return {
                    "payment_id": payment_id,
                    "order_id": body.order_id,
                    "method": "online",
                    "status": payment_status_value,
                    "amount": float(existing["amount"] or amt_dec),
                    "amount_paise": int((existing["amount_paise"] or 0)),
                    "currency": existing["currency"] or body.currency,
                    "razorpay_order_id": existing["rzp_order_id"] or existing["razorpay_order_id"],
                    "razorpay_payment_id": existing["razorpay_payment_id"],
                    "qr_id": existing["qr_id"],
                    "qr_image_url": existing["qr_image_url"],
                    "qr_image_content": existing["qr_image_content"],
                    "qr_close_by": existing["qr_close_by"].isoformat() if existing["qr_close_by"] else None,
                    "qr_status": existing["qr_status"],
                    "idempotent": True,
                }

            # If we already have a full intent + QR cached, short-circuit
            # without re-calling Razorpay.
            if existing["rzp_order_id"] and existing["qr_image_url"]:
                return {
                    "payment_id": payment_id,
                    "order_id": body.order_id,
                    "method": "online",
                    "status": payment_status_value,
                    "amount": float(existing["amount"] or amt_dec),
                    "amount_paise": int(existing["amount_paise"]),
                    "currency": existing["currency"] or body.currency,
                    "razorpay_order_id": existing["rzp_order_id"],
                    "razorpay_payment_id": existing["razorpay_payment_id"],
                    "qr_id": existing["qr_id"],
                    "qr_image_url": existing["qr_image_url"],
                    "qr_image_content": existing["qr_image_content"],
                    "qr_close_by": existing["qr_close_by"].isoformat() if existing["qr_close_by"] else None,
                    "qr_status": existing["qr_status"],
                    "idempotent": True,
                }

        # Create-or-replay the Razorpay intent (function is idempotent on
        # (merchant_id, internal_order_id)).
        # Customer & creator info gets stitched into the rzp_order + qr `notes`
        # so the Razorpay dashboard / webhooks show exactly which staff member
        # generated the QR and which customer it was generated for.
        eff_customer_name = body.customer_name or order_row["customer_name"]
        eff_customer_phone = body.customer_phone or order_row["customer_phone"]
        eff_customer_id = order_row["customer_id"]
        try:
            intent = await create_intent_for_order(
                merchant_id=user.restaurant_id,
                branch_id=order_row["branch_id"],
                internal_order_id=body.order_id,
                payment_id=payment_id,
                amount=amt_dec,
                currency=body.currency,
                receipt=order_row["order_number"],
                customer_name=eff_customer_name,
                customer_phone=eff_customer_phone,
                customer_id=str(eff_customer_id) if eff_customer_id is not None else None,
                created_by_user_id=order_row["created_by_user_id"],
                owner_user_id=getattr(user, "owner_id", None) or user.user_id,
                create_qr=True,
            )
        except PermissionError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail=f"merchant_not_settlement_ready: {exc}",
            )
        except Exception as exc:
            logger.error(
                "payments_initiate_intent_failed",
                order_id=body.order_id, error=str(exc),
            )
            raise HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail="razorpay intent creation failed",
            )

        await log_activity(
            user_id=user.user_id, branch_id=user.branch_id,
            action="payment.initiated", entity_type="payment", entity_id=payment_id,
            metadata={
                "order_id": body.order_id, "payment_mode": "online",
                "amount": float(amt_dec), "replayed": existing is not None,
            },
        )

        client = intent.to_client_dict()
        return {
            "payment_id": payment_id,
            "order_id": body.order_id,
            "method": "online",
            "status": payment_status_value,
            "amount": float(amt_dec),
            "amount_paise": client["amount"],
            "currency": client["currency"],
            "razorpay_order_id": client["razorpay_order_id"],
            "qr_id": client["qr_id"],
            "qr_image_url": client["qr_image_url"],
            "qr_image_content": client["qr_image_content"],
            "qr_close_by": client["qr_close_by"],
            "idempotent": existing is not None,
        }

    # ────────────────────────────────────────────────────────────────────
    # CASH / UPI / CARD / WALLET — legacy completion path.
    # ────────────────────────────────────────────────────────────────────
    try:
        result = await _svc.initiate_payment(
            user=user,
            order_id=body.order_id,
            method=method,
            amount=float(amt_dec),
        )
    except Exception as exc:
        # Surface the underlying error rather than a bare 500.
        logger.error(
            "payments_initiate_failed",
            order_id=body.order_id, method=method, error=str(exc),
        )
        raise

    await log_activity(
        user_id=user.user_id,
        branch_id=user.branch_id,
        action="payment.initiated",
        entity_type="payment",
        entity_id=result.get("payment_id") if isinstance(result, dict) else None,
        metadata={"order_id": body.order_id, "payment_mode": method, "amount": float(amt_dec)},
    )
    return result


@router.post("/verify")
async def verify_payment(
    body: VerifyPaymentIn,
    user: UserContext = Depends(require_permission("payment.create")),
):
    return await _svc.verify_razorpay_payment(
        razorpay_payment_id=body.razorpay_payment_id,
        razorpay_order_id=body.razorpay_order_id,
        razorpay_signature=body.razorpay_signature,
    )


@router.post("/refund")
async def refund_payment(
    body: RefundIn,
    user: UserContext = Depends(require_permission("payment.refund")),
):
    # Enforce max_refund_amount constraint from permission meta
    if body.amount is not None and user.permission_meta:
        max_refund = user.permission_meta.get("max_refund_amount")
        if max_refund is not None and body.amount > float(max_refund):
            from app.core.exceptions import ValidationError
            raise ValidationError(
                f"Refund amount {body.amount} exceeds max allowed {max_refund} for your role"
            )

    result = await _svc.initiate_refund(
        user=user,
        payment_id=body.payment_id,
        amount=body.amount,
        reason=body.reason,
    )
    await log_activity(
        user_id=user.user_id,
        branch_id=user.branch_id,
        action="payment.refund_issued",
        entity_type="payment",
        entity_id=body.payment_id,
        metadata={"amount": body.amount, "reason": body.reason},
    )
    return result
