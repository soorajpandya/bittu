"""
Razorpay payment-intent orchestration (Phase 2).

A "payment intent" = the bundle of resources we create on Razorpay when an
internal POS order with `payment_method='online'` is checked out:

    1. POST /v1/orders                  → razorpay_order_id, amount_due
    2. POST /v1/payments/qr_codes       → qr_id, image_url, image_content
    3. Persist mappings in our DB:
         rzp_orders, rzp_qr_codes, rzp_qr_order_links
       Update the existing payments row with razorpay_order_id.

All three resources are created OUTSIDE any open DB transaction (gateway
calls must never run inside a serializable txn). Persistence happens in a
single short transaction afterwards. If the gateway call partially succeeds
(e.g. rzp_order created but QR creation fails), we still persist the order
row so a retry can reuse the same razorpay_order_id via idempotency.

Idempotency:
    The caller supplies an `idempotency_key` (typically the internal
    payments.id). Both Razorpay calls use it (scoped per operation), and
    we look up `rzp_orders` by (merchant_id, internal_order_id) before
    re-creating — so retries are safe end-to-end.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional

from app.core.database import get_service_connection
from app.core.events import PAYMENT_INITIATED, DomainEvent, emit_and_publish
from app.core.logging import get_logger
from app.services.razorpay import orders as rzp_orders_api
from app.services.razorpay import qr_codes as rzp_qr_api
from app.services.razorpay.client import RazorpayError

logger = get_logger(__name__)

# ── tunables ──────────────────────────────────────────────────────────────
DEFAULT_QR_TTL_SECONDS = 30 * 60        # 30 minutes — POS UPI QR lifetime
DEFAULT_QR_TYPE = "upi_qr"
DEFAULT_QR_USAGE = "single_use"


# ── result type ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PaymentIntent:
    razorpay_order_id: str
    rzp_order_uuid: str
    amount_paise: int
    currency: str
    qr_id: Optional[str]
    qr_image_url: Optional[str]
    qr_image_content: Optional[str]   # raw upi:// payload (deep-link safe)
    qr_close_by: Optional[datetime]

    def to_client_dict(self) -> dict[str, Any]:
        """Shape returned to POS / customer-app."""
        return {
            "razorpay_order_id": self.razorpay_order_id,
            "amount": self.amount_paise,
            "currency": self.currency,
            "qr_id": self.qr_id,
            "qr_image_url": self.qr_image_url,
            "qr_image_content": self.qr_image_content,
            "qr_close_by": (
                self.qr_close_by.isoformat() if self.qr_close_by else None
            ),
        }


# ── public API ────────────────────────────────────────────────────────────


async def create_intent_for_order(
    *,
    merchant_id: str,
    branch_id: Optional[str],
    internal_order_id: str,
    payment_id: str,
    amount: Decimal,
    currency: str = "INR",
    receipt: Optional[str] = None,
    customer_name: Optional[str] = None,
    customer_phone: Optional[str] = None,
    create_qr: bool = True,
    qr_ttl_seconds: int = DEFAULT_QR_TTL_SECONDS,
    notes: Optional[Mapping[str, Any]] = None,
) -> PaymentIntent:
    """
    Idempotent: returns the existing intent if an `rzp_orders` row already
    exists for `(merchant_id, internal_order_id)`.
    """
    amount_paise = int((Decimal(amount) * 100).to_integral_value())
    if amount_paise <= 0:
        raise ValueError("intent amount must be > 0 paise")

    # ── 0. fast-path: replay existing intent ──
    existing = await _fetch_existing_intent(
        merchant_id=merchant_id, internal_order_id=internal_order_id
    )
    if existing is not None:
        logger.info(
            "rzp_intent_replay",
            merchant_id=merchant_id,
            internal_order_id=internal_order_id,
            razorpay_order_id=existing.razorpay_order_id,
        )
        return existing

    # ── 1. create Razorpay order (idempotent via X-Razorpay-Idempotency) ──
    note_payload: dict[str, Any] = {
        "internal_order_id": internal_order_id,
        "payment_id": payment_id,
        "merchant_id": merchant_id,
    }
    if branch_id:
        note_payload["branch_id"] = branch_id
    if notes:
        note_payload.update({k: str(v) for k, v in notes.items()})

    order_idem = f"rzp_order:{merchant_id}:{internal_order_id}"
    try:
        rzp_order = await rzp_orders_api.create_order(
            amount_paise=amount_paise,
            currency=currency,
            receipt=(receipt or internal_order_id)[:40],
            notes=note_payload,
            idempotency_key=order_idem,
            merchant_id=merchant_id,
        )
    except RazorpayError as exc:
        logger.error(
            "rzp_intent_order_failed",
            merchant_id=merchant_id,
            internal_order_id=internal_order_id,
            error=str(exc),
        )
        raise

    razorpay_order_id: str = rzp_order["id"]
    amount_due_paise = int(rzp_order.get("amount_due", amount_paise))

    # ── 2. persist rzp_orders + update payments.razorpay_order_id ──
    rzp_order_uuid = await _persist_rzp_order(
        merchant_id=merchant_id,
        branch_id=branch_id,
        internal_order_id=internal_order_id,
        payment_id=payment_id,
        razorpay_order_id=razorpay_order_id,
        receipt=rzp_order.get("receipt"),
        amount_paise=amount_paise,
        amount_due_paise=amount_due_paise,
        currency=currency,
        notes=note_payload,
        raw_response=rzp_order,
    )

    qr_id: Optional[str] = None
    qr_image_url: Optional[str] = None
    qr_image_content: Optional[str] = None
    qr_close_by_dt: Optional[datetime] = None

    # ── 3. create dynamic UPI QR (best-effort — order is the source of truth) ──
    if create_qr:
        close_by_dt = datetime.now(timezone.utc) + timedelta(seconds=int(qr_ttl_seconds))
        close_by_epoch = int(close_by_dt.timestamp())
        qr_idem = f"rzp_qr:{merchant_id}:{internal_order_id}"
        qr_name = f"Order {internal_order_id[:8]}"

        try:
            qr_resp = await rzp_qr_api.create_qr(
                name=qr_name[:30],
                amount_paise=amount_paise,
                description=(receipt or f"Order {internal_order_id}")[:100],
                fixed_amount=True,
                usage=DEFAULT_QR_USAGE,
                qr_type=DEFAULT_QR_TYPE,
                close_by=close_by_epoch,
                notes={
                    "internal_order_id": internal_order_id,
                    "razorpay_order_id": razorpay_order_id,
                    "merchant_id": merchant_id,
                },
                idempotency_key=qr_idem,
                merchant_id=merchant_id,
            )
            qr_id = qr_resp["id"]
            qr_image_url = qr_resp.get("image_url")
            qr_image_content = qr_resp.get("image_content")
            qr_close_by_dt = close_by_dt

            await _persist_qr_and_link(
                merchant_id=merchant_id,
                branch_id=branch_id,
                internal_order_id=internal_order_id,
                rzp_order_uuid=rzp_order_uuid,
                razorpay_order_id=razorpay_order_id,
                qr_resp=qr_resp,
                amount_paise=amount_paise,
                close_by_dt=close_by_dt,
            )
        except RazorpayError as exc:
            # QR is optional — caller can fall back to checkout-form / hosted
            # page using just the razorpay_order_id.
            logger.warning(
                "rzp_intent_qr_failed",
                merchant_id=merchant_id,
                internal_order_id=internal_order_id,
                razorpay_order_id=razorpay_order_id,
                error=str(exc),
            )

    intent = PaymentIntent(
        razorpay_order_id=razorpay_order_id,
        rzp_order_uuid=rzp_order_uuid,
        amount_paise=amount_paise,
        currency=currency,
        qr_id=qr_id,
        qr_image_url=qr_image_url,
        qr_image_content=qr_image_content,
        qr_close_by=qr_close_by_dt,
    )

    # ── 4. domain event (best-effort) ──
    try:
        await emit_and_publish(DomainEvent(
            event_type=PAYMENT_INITIATED,
            payload={
                "merchant_id": merchant_id,
                "internal_order_id": internal_order_id,
                "payment_id": payment_id,
                "razorpay_order_id": razorpay_order_id,
                "amount_paise": amount_paise,
                "qr_id": qr_id,
            },
        ))
    except Exception as exc:                          # pragma: no cover
        logger.warning("rzp_intent_emit_failed", error=str(exc))

    return intent


async def get_intent(
    *, merchant_id: str, internal_order_id: str
) -> Optional[PaymentIntent]:
    return await _fetch_existing_intent(
        merchant_id=merchant_id, internal_order_id=internal_order_id
    )


# ── private helpers ───────────────────────────────────────────────────────


async def _fetch_existing_intent(
    *, merchant_id: str, internal_order_id: str
) -> Optional[PaymentIntent]:
    async with get_service_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                o.id::text          AS rzp_order_uuid,
                o.razorpay_order_id,
                o.amount_paise,
                o.currency,
                q.qr_id,
                q.image_url,
                q.image_content,
                q.close_by
            FROM rzp_orders o
            LEFT JOIN rzp_qr_order_links l
                   ON l.rzp_order_uuid = o.id
                  AND l.is_primary = TRUE
            LEFT JOIN rzp_qr_codes q
                   ON q.qr_id = l.qr_id
            WHERE o.merchant_id = $1
              AND o.internal_order_id = $2
            ORDER BY o.created_at DESC
            LIMIT 1
            """,
            merchant_id,
            internal_order_id,
        )
    if not row:
        return None
    return PaymentIntent(
        razorpay_order_id=row["razorpay_order_id"],
        rzp_order_uuid=row["rzp_order_uuid"],
        amount_paise=int(row["amount_paise"]),
        currency=row["currency"],
        qr_id=row["qr_id"],
        qr_image_url=row["image_url"],
        qr_image_content=row["image_content"],
        qr_close_by=row["close_by"],
    )


async def _persist_rzp_order(
    *,
    merchant_id: str,
    branch_id: Optional[str],
    internal_order_id: str,
    payment_id: str,
    razorpay_order_id: str,
    receipt: Optional[str],
    amount_paise: int,
    amount_due_paise: int,
    currency: str,
    notes: Mapping[str, Any],
    raw_response: Mapping[str, Any],
) -> str:
    """Returns rzp_orders.id (uuid as text)."""
    async with get_service_connection() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO rzp_orders (
                    merchant_id, branch_id, internal_order_id,
                    razorpay_order_id, receipt,
                    amount_paise, amount_paid_paise, amount_due_paise,
                    currency, status, notes, raw_response
                ) VALUES (
                    $1, $2, $3,
                    $4, $5,
                    $6, 0, $7,
                    $8, 'created'::rzp_order_state, $9::jsonb, $10::jsonb
                )
                ON CONFLICT (merchant_id, internal_order_id) DO UPDATE
                    SET raw_response = EXCLUDED.raw_response,
                        updated_at   = NOW()
                RETURNING id::text
                """,
                merchant_id,
                branch_id,
                internal_order_id,
                razorpay_order_id,
                receipt,
                amount_paise,
                amount_due_paise,
                currency,
                json.dumps(dict(notes)),
                json.dumps(dict(raw_response)),
            )

            # Mirror razorpay_order_id onto the existing payments row so
            # webhook lookups by razorpay_order_id resolve correctly.
            await conn.execute(
                """
                UPDATE payments
                   SET razorpay_order_id = $1,
                       updated_at = NOW()
                 WHERE id = $2::uuid
                   AND restaurant_id = $3::uuid
                """,
                razorpay_order_id,
                payment_id,
                merchant_id,
            )

    return row["id"]


async def _persist_qr_and_link(
    *,
    merchant_id: str,
    branch_id: Optional[str],
    internal_order_id: str,
    rzp_order_uuid: str,
    razorpay_order_id: str,
    qr_resp: Mapping[str, Any],
    amount_paise: int,
    close_by_dt: datetime,
) -> None:
    qr_id = qr_resp["id"]
    async with get_service_connection() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO rzp_qr_codes (
                    qr_id, merchant_id, branch_id, name, type, usage,
                    fixed_amount, amount_paise, description,
                    image_url, image_content, status, close_by,
                    notes, raw_response
                ) VALUES (
                    $1, $2, $3, $4, $5, $6,
                    TRUE, $7, $8,
                    $9, $10, 'active'::rzp_qr_state, $11,
                    $12::jsonb, $13::jsonb
                )
                ON CONFLICT (qr_id) DO UPDATE
                    SET raw_response = EXCLUDED.raw_response,
                        updated_at   = NOW()
                """,
                qr_id,
                merchant_id,
                branch_id,
                qr_resp.get("name"),
                qr_resp.get("type", DEFAULT_QR_TYPE),
                qr_resp.get("usage", DEFAULT_QR_USAGE),
                int(qr_resp.get("payment_amount", amount_paise) or amount_paise),
                qr_resp.get("description"),
                qr_resp.get("image_url"),
                qr_resp.get("image_content"),
                close_by_dt,
                json.dumps(dict(qr_resp.get("notes") or {})),
                json.dumps(dict(qr_resp)),
            )

            await conn.execute(
                """
                INSERT INTO rzp_qr_order_links (
                    qr_id, rzp_order_uuid, razorpay_order_id,
                    internal_order_id, merchant_id, is_primary
                ) VALUES ($1, $2::uuid, $3, $4, $5, TRUE)
                ON CONFLICT (qr_id, internal_order_id) DO NOTHING
                """,
                qr_id,
                rzp_order_uuid,
                razorpay_order_id,
                internal_order_id,
                merchant_id,
            )
