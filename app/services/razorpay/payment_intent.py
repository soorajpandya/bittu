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
from app.services.razorpay.qr_codes import prefer_bittu_qr_image_url
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
        razorpay_qr_image_url = self.qr_image_url
        qr_image_url = prefer_bittu_qr_image_url(
            upi_intent=self.qr_image_content,
            razorpay_image_url=razorpay_qr_image_url,
        )
        return {
            "razorpay_order_id": self.razorpay_order_id,
            "amount": self.amount_paise,
            "currency": self.currency,
            "qr_id": self.qr_id,
            "qr_image_url": qr_image_url,
            "razorpay_qr_image_url": razorpay_qr_image_url,
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
    customer_id: Optional[str] = None,
    created_by_user_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    create_qr: bool = True,
    qr_ttl_seconds: int = DEFAULT_QR_TTL_SECONDS,
    notes: Optional[Mapping[str, Any]] = None,
) -> PaymentIntent:
    """
    Idempotent: returns the existing intent if an `rzp_orders` row already
    exists for `(merchant_id, internal_order_id)`.

    Identity context (who generated the QR / who is paying) is embedded into
    both the Razorpay order `notes` and the QR `notes` so it shows up in the
    Razorpay dashboard, webhooks, and our `rzp_orders.notes` / `rzp_qr_codes.notes`
    JSONB columns.
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

    # ── 0.5. settlement-readiness gate ──
    # If the merchant has opted into Route, their linked account + product
    # configuration MUST be activated before we accept new payments —
    # otherwise the captured funds would sit on the Bittu master account
    # with no path to the merchant's bank. Legacy merchants (no Route row)
    # pass through. Maps to HTTP 409 at the API boundary.
    from app.services.razorpay.route_service import rzp_route_service as _route_gate
    await _route_gate.assert_settlement_ready(merchant_id=merchant_id)

    # ── 1. create Razorpay order (idempotent via X-Razorpay-Idempotency) ──
    # Razorpay limits each `notes` value to a string; cap field lengths so we
    # never get rejected at the gateway.
    def _s(v: Any, cap: int = 200) -> str:
        return str(v)[:cap]

    note_payload: dict[str, Any] = {
        "internal_order_id": internal_order_id,
        "payment_id": payment_id,
        "merchant_id": merchant_id,
    }
    if branch_id:
        note_payload["branch_id"] = branch_id
    if owner_user_id:
        note_payload["owner_user_id"] = _s(owner_user_id)
    if created_by_user_id:
        note_payload["created_by_user_id"] = _s(created_by_user_id)
    if customer_id:
        note_payload["customer_id"] = _s(customer_id)
    if customer_name:
        note_payload["customer_name"] = _s(customer_name, 100)
    if customer_phone:
        note_payload["customer_phone"] = _s(customer_phone, 20)
    if notes:
        note_payload.update({k: _s(v) for k, v in notes.items()})

    order_idem = f"rzp_order:{merchant_id}:{internal_order_id}"

    # ── 1a. build Route transfers[] split if merchant is activated ──
    # When the merchant's linked account + product are both activated, ask
    # Razorpay to atomically split the captured payment into the merchant's
    # linked account at capture time. Bittu keeps a fixed 1.65% platform fee;
    # merchant_share is floored to whole paise so the sum never exceeds
    # the gross amount.
    #
    # If the merchant share would be below Razorpay's per-transfer minimum
    # (₹1 = 100 paise) we skip the split — the payment still lands on the
    # platform account and the merchant ledger projection will surface it
    # as "pending" until an out-of-band reconciler creates a transfer.
    transfers_payload: Optional[list[dict]] = None
    linked_account_id = await _route_gate.get_active_linked_account_id(merchant_id)
    if linked_account_id:
        # Bittu keeps a FIXED 1.65% platform fee; merchant share is
        # gross - bittu_fee - estimated_rzp_charge. The payment method is
        # not yet known at order creation, so the estimate uses the default
        # rate and is trued-up later at settlement.
        from app.services.razorpay.fee_policy import provisional_merchant_transfer_paise
        merchant_share_paise, _bittu_fee_paise, _est_rzp_paise = (
            provisional_merchant_transfer_paise(amount_paise, None)
        )
        if merchant_share_paise >= 100:
            transfers_payload = [{
                "account":  linked_account_id,
                "amount":   int(merchant_share_paise),
                "currency": currency,
                "notes": {
                    "merchant_id":       merchant_id,
                    "internal_order_id": internal_order_id,
                    "razorpay_payment_id_for": "auto_route_at_capture",
                    "bittu_fee_paise":   str(_bittu_fee_paise),
                    "est_rzp_paise":     str(_est_rzp_paise),
                },
                "linked_account_notes": ["merchant_id", "internal_order_id"],
                "on_hold": False,
            }]
            logger.info(
                "rzp_intent_with_transfers",
                merchant_id=merchant_id,
                internal_order_id=internal_order_id,
                linked_account_id=linked_account_id,
                gross_paise=amount_paise,
                merchant_share_paise=int(merchant_share_paise),
                commission_paise=int(amount_paise - merchant_share_paise),
            )
        else:
            logger.warning(
                "rzp_intent_transfer_skipped_too_small",
                merchant_id=merchant_id,
                gross_paise=amount_paise,
                merchant_share_paise=int(merchant_share_paise),
            )

    try:
        rzp_order = await rzp_orders_api.create_order(
            amount_paise=amount_paise,
            currency=currency,
            receipt=(receipt or internal_order_id)[:40],
            notes=note_payload,
            idempotency_key=order_idem,
            merchant_id=merchant_id,
            transfers=transfers_payload,
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
        # QR `name` is the human label shown in the Razorpay dashboard list.
        # Prefer customer name so the owner can scan the list and immediately
        # see WHO each QR was generated for. Razorpay caps `name` at 30 chars.
        if customer_name:
            qr_name = f"{customer_name} #{internal_order_id[:6]}"
        else:
            qr_name = f"Order {internal_order_id[:8]}"

        # QR-level notes mirror the order notes + add gateway-specific keys.
        qr_notes: dict[str, Any] = {
            "internal_order_id": internal_order_id,
            "razorpay_order_id": razorpay_order_id,
            "merchant_id": merchant_id,
        }
        if branch_id:
            qr_notes["branch_id"] = branch_id
        if owner_user_id:
            qr_notes["owner_user_id"] = _s(owner_user_id)
        if created_by_user_id:
            qr_notes["created_by_user_id"] = _s(created_by_user_id)
        if customer_id:
            qr_notes["customer_id"] = _s(customer_id)
        if customer_name:
            qr_notes["customer_name"] = _s(customer_name, 100)
        if customer_phone:
            qr_notes["customer_phone"] = _s(customer_phone, 20)

        try:
            qr_resp = await rzp_qr_api.create_qr(
                name=qr_name[:30],
                amount_paise=amount_paise,
                description=(receipt or f"Order {internal_order_id}")[:100],
                fixed_amount=True,
                usage=DEFAULT_QR_USAGE,
                qr_type=DEFAULT_QR_TYPE,
                close_by=close_by_epoch,
                notes=qr_notes,
                idempotency_key=qr_idem,
                merchant_id=merchant_id,
            )
            qr_id = qr_resp["id"]
            qr_image_url = qr_resp.get("image_url")
            qr_image_content = qr_resp.get("image_content")
            qr_close_by_dt = close_by_dt

            if not qr_image_content and qr_image_url:
                resolved_intent, decode_source = await rzp_qr_api.resolve_upi_intent_for_qr(
                    upi_intent=None,
                    image_url=qr_image_url,
                    qr_id=qr_id,
                    merchant_id=merchant_id,
                    fixed_amount=True,
                    payment_amount_paise=amount_paise,
                    payer_name="Bittu POS",
                )
                if resolved_intent:
                    qr_image_content = resolved_intent
                    qr_resp = dict(qr_resp)
                    qr_resp["image_content"] = resolved_intent
                    logger.info(
                        "rzp_qr_intent_extracted_from_image",
                        merchant_id=merchant_id,
                        internal_order_id=internal_order_id,
                        qr_id=qr_id,
                        source=decode_source,
                    )

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
