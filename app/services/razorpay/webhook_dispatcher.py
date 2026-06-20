"""
Razorpay webhook event dispatcher (Phase 3).

Routes incoming, signature-verified webhook envelopes to per-event handlers.
The HTTP route in `app/api/v1/webhooks.py` defers all Razorpay events to
`dispatch_event()` here.

Two-tier idempotency
--------------------
1. Transport-level: `app.core.webhook_security.verify_and_register_webhook`
   short-circuits replays via `UNIQUE(gateway, event_id)` on
   `payment_webhook_events`. Phase 3 fixes the event_id extractor to use
   the `X-Razorpay-Event-Id` header (the actual event id; the previous
   extractor used the *payment* id, which collided across event types
   for the same payment).
2. Business-level: every handler uses `INSERT ... ON CONFLICT DO UPDATE`
   on the appropriate `rzp_*` table and gates `payments` / `orders`
   updates on the current row state, so resends and out-of-order
   deliveries cannot corrupt the ledger.

Cross-merchant ops
------------------
Gateway tables (`rzp_*`) are RLS-disabled by design — webhooks do not
arrive with a merchant context, only the entity ids. The dispatcher
uses `get_service_connection()` for all reads/writes. Once an internal
`merchant_id` is resolved (via `rzp_orders` / `rzp_qr_codes`), it is
forwarded to the merchant_ledger and escrow integrations.

Money flow
----------
Only `payment.captured` (and `qr_code.credited`'s embedded payment) drive
the canonical merchant-ledger CREDIT + escrow HOLD. `payment.authorized`
records but does NOT credit (Razorpay still allows the merchant to
explicitly capture or void; in our flow we use auto-capture, so
`authorized → captured` lands within seconds). Refunds and disputes have
their own services (Phase 7) that own the merchant_ledger debits — the
webhook simply mirrors the gateway's view.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional

from app.core.database import get_service_connection
from app.core.events import (
    DomainEvent,
    PAYMENT_COMPLETED,
    PAYMENT_FAILED,
    PAYMENT_REFUNDED,
    PAYMENT_EXPIRED,
    emit_and_publish,
)
from app.core.logging import get_logger
from app.services.razorpay import webhooks as _events  # event-name constants

logger = get_logger(__name__)


# ════════════════════════════════════════════════════════════════════════
# Static QR Payment Module — additive side-effect hook
# ════════════════════════════════════════════════════════════════════════
# Mirrors any `payment.*` event whose payload is tied to a multi-use
# Static QR (see ``app.services.razorpay.static_qr_service``). This is a
# fire-and-forget call that runs AFTER the existing order/route handling
# is complete and never raises — the order-driven flow is unaffected
# when the payment is not a static-QR payment.
async def _static_qr_webhook_sideeffect(event_name: str, envelope: dict) -> None:
    try:
        from app.services.razorpay import static_qr_service as _static_qr
        await _static_qr.handle_webhook_payment_event(envelope, event_name=event_name)
    except Exception:  # noqa: BLE001
        logger.exception("static_qr_webhook_sideeffect_failed", event_name=event_name)


# ════════════════════════════════════════════════════════════════════════
# Public entry point
# ════════════════════════════════════════════════════════════════════════

async def dispatch_event(
    *,
    event: str,
    envelope: dict,
    signature: Optional[str] = None,
) -> dict:
    """
    Route a Razorpay webhook envelope to the correct handler.

    `envelope` is the FULL parsed JSON body (top-level keys: event,
    account_id, contains, payload, created_at). Handlers slice their own
    entity from `envelope["payload"]`.

    Returns `{"status": "processed"|"unhandled"|"skipped", "event": ...}`.
    Raises only on infrastructure errors (DB down, etc.) so that the
    HTTP route's `mark_processed("failed")` path fires for retry.
    """
    handler = _HANDLERS.get(event)
    if handler is None:
        logger.info("rzp_webhook_unhandled", event_name=event)
        return {"status": "unhandled", "event": event}

    try:
        result = await handler(envelope, signature)
    except Exception:
        logger.exception("rzp_webhook_handler_failed", event_name=event)
        raise
    return {"status": "processed", "event": event, **(result or {})}


# ════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════

def _entity(envelope: dict, *path: str) -> dict:
    """Pluck `envelope['payload'][path[0]][path[1]]...['entity']`. Empty dict if missing."""
    cur: Any = envelope.get("payload") or {}
    for p in path:
        cur = (cur or {}).get(p) or {}
    return (cur or {}).get("entity") or {}


async def _resolve_order_context_by_rzp_order(
    rzp_order_id: Optional[str],
) -> Optional[dict]:
    """Look up our internal order linked to a Razorpay order id."""
    if not rzp_order_id:
        return None
    async with get_service_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT id::text                    AS rzp_order_uuid,
                   merchant_id::text           AS merchant_id,
                   branch_id::text             AS branch_id,
                   internal_order_id::text     AS internal_order_id
            FROM rzp_orders
            WHERE razorpay_order_id = $1
            LIMIT 1
            """,
            rzp_order_id,
        )
    return dict(row) if row else None


async def _resolve_order_context_by_qr(
    qr_id: Optional[str],
) -> Optional[dict]:
    """For QR-driven payments, derive the linked internal order via rzp_qr_order_links."""
    if not qr_id:
        return None
    async with get_service_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT l.internal_order_id::text   AS internal_order_id,
                   l.merchant_id::text         AS merchant_id,
                   l.razorpay_order_id         AS razorpay_order_id,
                   l.rzp_order_uuid::text      AS rzp_order_uuid,
                   q.branch_id::text           AS branch_id
            FROM rzp_qr_order_links l
            JOIN rzp_qr_codes q ON q.qr_id = l.qr_id
            WHERE l.qr_id = $1 AND l.is_primary = TRUE
            ORDER BY l.created_at DESC
            LIMIT 1
            """,
            qr_id,
        )
    return dict(row) if row else None


async def _find_payment_id_for_rzp_order(
    rzp_order_id: Optional[str],
) -> Optional[str]:
    """Look up our internal payments.id for a given razorpay_order_id."""
    if not rzp_order_id:
        return None
    async with get_service_connection() as conn:
        row = await conn.fetchrow(
            "SELECT id::text AS id FROM payments WHERE razorpay_order_id = $1 LIMIT 1",
            rzp_order_id,
        )
    return row["id"] if row else None


# ════════════════════════════════════════════════════════════════════════
# Persistence helpers (idempotent UPSERTs)
# ════════════════════════════════════════════════════════════════════════

async def _upsert_rzp_payment(
    *,
    entity: dict,
    envelope: dict,
    signature: Optional[str],
    merchant_id: Optional[str],
    branch_id: Optional[str],
    internal_order_id: Optional[str],
    rzp_order_uuid: Optional[str],
    status: str,
    captured: bool,
) -> Optional[str]:
    """UPSERT into the partitioned `rzp_payments` + cross-partition index.

    Returns the payment row uuid.
    """
    rzp_payment_id = entity.get("id")
    if not rzp_payment_id or not merchant_id:
        # Without a merchant_id we can't satisfy NOT NULL — orphan event.
        # Caller logs and returns; recon engine picks these up later.
        return None

    raw = json.dumps(envelope)
    notes = json.dumps(entity.get("notes") or {})
    acquirer = json.dumps(entity.get("acquirer_data") or {}) if entity.get("acquirer_data") else None

    async with get_service_connection() as conn:
        # 1. Try to find an existing row via the cross-partition index.
        existing = await conn.fetchrow(
            "SELECT payment_uuid::text AS id FROM rzp_payments_index WHERE razorpay_payment_id = $1",
            rzp_payment_id,
        )
        if existing:
            payment_uuid = existing["id"]
            # 2. Update the existing partition row (state-forward).
            async with conn.transaction():
                prev = await conn.fetchrow(
                    """
                    UPDATE rzp_payments
                    SET status            = $1::rzp_payment_state,
                        captured          = $2,
                        captured_at       = COALESCE(captured_at,
                                                     CASE WHEN $2 THEN NOW() ELSE NULL END),
                        fee_paise         = COALESCE($3, fee_paise),
                        tax_paise         = COALESCE($4, tax_paise),
                        method            = COALESCE($5, method),
                        upi_vpa           = COALESCE($6, upi_vpa),
                        bank_reference    = COALESCE($7, bank_reference),
                        acquirer_data     = COALESCE($8::jsonb, acquirer_data),
                        error_code        = COALESCE($9, error_code),
                        error_description = COALESCE($10, error_description),
                        raw_payload       = $11::jsonb,
                        signature         = COALESCE($12, signature),
                        notes             = $13::jsonb,
                        updated_at        = NOW()
                    WHERE id = $14::uuid
                    RETURNING status::text AS new_status
                    """,
                    status, captured,
                    entity.get("fee"), entity.get("tax"),
                    entity.get("method"), entity.get("vpa"),
                    entity.get("bank") or entity.get("bank_transaction_id"),
                    acquirer,
                    entity.get("error_code"), entity.get("error_description"),
                    raw, signature, notes, payment_uuid,
                )
                # 3. Append to status log (best-effort, append-only).
                if prev and prev["new_status"]:
                    try:
                        await conn.execute(
                            """
                            INSERT INTO rzp_payment_status_log
                                (razorpay_payment_id, from_status, to_status, raw_payload)
                            VALUES ($1, NULL, $2::rzp_payment_state, $3::jsonb)
                            """,
                            rzp_payment_id, status, raw,
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("rzp_payment_status_log_insert_failed",
                                         payment_id=rzp_payment_id)
            return payment_uuid

        # No existing row: insert into partition + index in a single txn.
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO rzp_payments (
                    razorpay_payment_id, razorpay_order_id, rzp_order_uuid,
                    merchant_id, branch_id, internal_order_id,
                    amount_paise, fee_paise, tax_paise, currency,
                    method, upi_vpa, bank_reference, acquirer_data,
                    status, error_code, error_description,
                    captured, captured_at,
                    raw_payload, signature, notes
                ) VALUES (
                    $1, $2, $3::uuid,
                    $4::uuid, $5::uuid, $6::uuid,
                    $7, $8, $9, $10,
                    $11, $12, $13, $14::jsonb,
                    $15::rzp_payment_state, $16, $17,
                    $18, CASE WHEN $18 THEN NOW() ELSE NULL END,
                    $19::jsonb, $20, $21::jsonb
                )
                RETURNING id::text AS id
                """,
                rzp_payment_id,
                entity.get("order_id"),
                rzp_order_uuid,
                merchant_id, branch_id, internal_order_id,
                int(entity.get("amount") or 0),
                entity.get("fee"), entity.get("tax"),
                entity.get("currency") or "INR",
                entity.get("method"),
                entity.get("vpa"),
                entity.get("bank") or entity.get("bank_transaction_id"),
                acquirer,
                status,
                entity.get("error_code"), entity.get("error_description"),
                captured,
                raw, signature, notes,
            )
            payment_uuid = row["id"]
            # Cross-partition unique index — if a parallel handler beat us,
            # swallow the conflict and re-fetch.
            try:
                await conn.execute(
                    """
                    INSERT INTO rzp_payments_index (razorpay_payment_id, payment_uuid)
                    VALUES ($1, $2::uuid)
                    """,
                    rzp_payment_id, payment_uuid,
                )
            except Exception:  # noqa: BLE001
                logger.warning("rzp_payments_index_conflict",
                               razorpay_payment_id=rzp_payment_id)
            try:
                await conn.execute(
                    """
                    INSERT INTO rzp_payment_status_log
                        (razorpay_payment_id, from_status, to_status, raw_payload)
                    VALUES ($1, NULL, $2::rzp_payment_state, $3::jsonb)
                    """,
                    rzp_payment_id, status, raw,
                )
            except Exception:  # noqa: BLE001
                logger.exception("rzp_payment_status_log_insert_failed",
                                 payment_id=rzp_payment_id)
        return payment_uuid


async def _mark_payments_row(
    *,
    rzp_order_id: Optional[str],
    rzp_payment_id: Optional[str],
    new_status: str,
    expected_current: tuple[str, ...],
) -> Optional[str]:
    """
    State-forward UPDATE of the canonical `payments` row.

    `expected_current` gates the transition (e.g. only flip pending→completed).
    Returns the internal payments.id if a row was updated, else None.
    """
    if not rzp_order_id:
        return None
    async with get_service_connection() as conn:
        if new_status == "completed":
            row = await conn.fetchrow(
                """
                UPDATE payments
                SET status              = 'completed'::payment_status,
                    razorpay_payment_id = COALESCE(razorpay_payment_id, $1),
                    paid_at             = COALESCE(paid_at, NOW()),
                    updated_at          = NOW()
                WHERE razorpay_order_id = $2
                  AND status::text = ANY($3::text[])
                RETURNING id::text       AS id,
                          order_id::text AS order_id,
                          restaurant_id::text AS merchant_id,
                          branch_id::text AS branch_id,
                          amount         AS amount,
                          method         AS method
                """,
                rzp_payment_id, rzp_order_id, list(expected_current),
            )
        elif new_status == "failed":
            row = await conn.fetchrow(
                """
                UPDATE payments
                SET status     = 'failed'::payment_status,
                    updated_at = NOW()
                WHERE razorpay_order_id = $1
                  AND status::text = ANY($2::text[])
                RETURNING id::text       AS id,
                          order_id::text AS order_id,
                          restaurant_id::text AS merchant_id
                """,
                rzp_order_id, list(expected_current),
            )
        elif new_status == "initiated":
            row = await conn.fetchrow(
                """
                UPDATE payments
                SET status              = 'initiated'::payment_status,
                    razorpay_payment_id = COALESCE(razorpay_payment_id, $1),
                    updated_at          = NOW()
                WHERE razorpay_order_id = $2
                  AND status::text = ANY($3::text[])
                RETURNING id::text       AS id
                """,
                rzp_payment_id, rzp_order_id, list(expected_current),
            )
        else:
            return None
    return dict(row) if row else None


async def _confirm_order(internal_order_id: Optional[str]) -> None:
    if not internal_order_id:
        return
    async with get_service_connection() as conn:
        await conn.execute(
            """
            UPDATE orders
            SET status     = 'Confirmed',
                updated_at = NOW()
            WHERE id = $1::uuid
              AND status NOT IN ('Confirmed', 'Completed', 'Cancelled', 'Served', 'Delivered')
            """,
            internal_order_id,
        )


async def _bump_rzp_order_paid(
    *,
    rzp_order_id: Optional[str],
    amount_paid_paise: Optional[int] = None,
) -> None:
    """Mark an rzp_order as `paid` and refresh amount_paid/due."""
    if not rzp_order_id:
        return
    async with get_service_connection() as conn:
        await conn.execute(
            """
            UPDATE rzp_orders
            SET status            = 'paid'::rzp_order_state,
                amount_paid_paise = COALESCE($2, amount_paid_paise, amount_paise),
                amount_due_paise  = GREATEST(amount_paise - COALESCE($2, amount_paise), 0),
                updated_at        = NOW()
            WHERE razorpay_order_id = $1
              AND status <> 'paid'::rzp_order_state
            """,
            rzp_order_id, amount_paid_paise,
        )


# ════════════════════════════════════════════════════════════════════════
# Payment handlers
# ════════════════════════════════════════════════════════════════════════

async def _handle_payment_authorized(envelope: dict, signature: Optional[str]) -> dict:
    entity = _entity(envelope, "payment")
    rzp_order_id = entity.get("order_id")
    ctx = await _resolve_order_context_by_rzp_order(rzp_order_id)

    payment_uuid = await _upsert_rzp_payment(
        entity=entity, envelope=envelope, signature=signature,
        merchant_id=(ctx or {}).get("merchant_id"),
        branch_id=(ctx or {}).get("branch_id"),
        internal_order_id=(ctx or {}).get("internal_order_id"),
        rzp_order_uuid=(ctx or {}).get("rzp_order_uuid"),
        status="authorized", captured=False,
    )
    # Move our payments row to `initiated` so the POS sees movement
    # without yet treating it as money in hand.
    await _mark_payments_row(
        rzp_order_id=rzp_order_id,
        rzp_payment_id=entity.get("id"),
        new_status="initiated",
        expected_current=("pending",),
    )
    await _static_qr_webhook_sideeffect(_events.EVENT_PAYMENT_AUTHORIZED, envelope)
    return {"rzp_payment_uuid": payment_uuid}


async def _handle_payment_captured(envelope: dict, signature: Optional[str]) -> dict:
    entity = _entity(envelope, "payment")
    rzp_order_id = entity.get("order_id")
    rzp_payment_id = entity.get("id")
    amount_paise = int(entity.get("amount") or 0)

    ctx = await _resolve_order_context_by_rzp_order(rzp_order_id) or {}
    # QR-driven captures arrive with order_id=None. Razorpay does NOT echo a
    # qr_id on the standalone payment.captured envelope either, so fall back
    # to the payment notes we stamped at QR creation (merchant_id,
    # internal_order_id, branch_id) to recover context. Without this the
    # entire money pipeline (ledger credit, escrow, auto-split transfer)
    # silently skips and the merchant's payout is stranded on Bittu's
    # master account.
    if not ctx:
        _notes = entity.get("notes") or {}
        if isinstance(_notes, dict) and _notes.get("merchant_id"):
            ctx = {
                "merchant_id":       _notes.get("merchant_id"),
                "branch_id":         _notes.get("branch_id"),
                "internal_order_id": _notes.get("internal_order_id"),
                "rzp_order_uuid":    None,
            }
            logger.info(
                "rzp_webhook_captured_ctx_from_notes",
                razorpay_payment_id=rzp_payment_id,
                merchant_id=ctx["merchant_id"],
                internal_order_id=ctx["internal_order_id"],
            )
    merchant_id = ctx.get("merchant_id")
    branch_id = ctx.get("branch_id")
    internal_order_id = ctx.get("internal_order_id")

    payment_uuid = await _upsert_rzp_payment(
        entity=entity, envelope=envelope, signature=signature,
        merchant_id=merchant_id, branch_id=branch_id,
        internal_order_id=internal_order_id,
        rzp_order_uuid=ctx.get("rzp_order_uuid"),
        status="captured", captured=True,
    )

    # Flip canonical payments row pending|initiated → completed.
    payment_row = await _mark_payments_row(
        rzp_order_id=rzp_order_id,
        rzp_payment_id=rzp_payment_id,
        new_status="completed",
        expected_current=("pending", "initiated"),
    )

    # Confirm the order.
    await _confirm_order(internal_order_id)
    await _bump_rzp_order_paid(rzp_order_id=rzp_order_id, amount_paid_paise=amount_paise)

    # Money pipeline: ledger credit + escrow hold (idempotent on payment_id).
    if payment_row and merchant_id:
        amount_decimal = Decimal(str(payment_row["amount"]))
        try:
            from app.services import merchant_ledger_integration
            await merchant_ledger_integration.post_payment_received(
                merchant_id=merchant_id,
                payment_id=payment_row["id"],
                amount=amount_decimal,
                method=payment_row.get("method") or entity.get("method"),
                order_id=payment_row.get("order_id"),
                branch_id=branch_id,
                bank_reference=entity.get("bank") or entity.get("bank_transaction_id"),
                extra_metadata={
                    "razorpay_payment_id": rzp_payment_id,
                    "razorpay_order_id":   rzp_order_id,
                    "source":              "razorpay_webhook",
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("rzp_webhook_ledger_credit_failed",
                             payment_id=payment_row["id"])
        try:
            from app.services import escrow_integration
            await escrow_integration.hold_payment_in_escrow(
                merchant_id=merchant_id,
                payment_id=payment_row["id"],
                amount=amount_decimal,
                method=payment_row.get("method") or entity.get("method"),
                order_id=payment_row.get("order_id"),
                branch_id=branch_id,
                extra_metadata={
                    "razorpay_payment_id": rzp_payment_id,
                    "razorpay_order_id":   rzp_order_id,
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("rzp_webhook_escrow_hold_failed",
                             payment_id=payment_row["id"])

        # Pre-generate the ElevenLabs payment-confirmation MP3 so the FE can
        # play it via <audio src="voice_url"> with no auth header / extra
        # fetch. Best-effort: empty url on any failure.
        voice_url = ""
        try:
            from app.services.elevenlabs_service import ElevenLabsService
            voice_url = await ElevenLabsService().ensure_payment_voice_file(
                token=rzp_payment_id,
                amount=float(amount_decimal),
                language="en",
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "rzp_voice_prepare_failed",
                razorpay_payment_id=rzp_payment_id,
            )
            voice_url = ""

        try:
            await emit_and_publish(DomainEvent(
                event_type=PAYMENT_COMPLETED,
                payload={
                    "payment_id":           payment_row["id"],
                    "order_id":             payment_row.get("order_id"),
                    "merchant_id":          merchant_id,
                    "razorpay_payment_id":  rzp_payment_id,
                    "razorpay_order_id":    rzp_order_id,
                    "amount":               float(amount_decimal),
                    "method":               payment_row.get("method"),
                    "source":               "webhook",
                    # ElevenLabs voice confirmation — FE plays voice_url
                    # directly via <audio src=...>; no auth required.
                    "should_play_voice":    bool(voice_url),
                    "voice_url":            voice_url or None,
                    "voice_amount_rupees":  float(amount_decimal),
                    "voice_language":       "en",
                },
            ))
        except Exception:  # noqa: BLE001
            logger.exception("rzp_webhook_event_emit_failed",
                             payment_id=payment_row["id"])

        # ── Lightning-fast WS push (in-process, no Redis required) ──
        # Direct broadcast for clients connected to THIS worker so the FE
        # doesn't have to wait for the next 2s poll tick. Cross-worker
        # delivery still rides emit_to_redis above.
        try:
            from app.realtime import push_local
            await push_local(
                "payment.captured",
                {
                    "order_id":            payment_row.get("order_id"),
                    "payment_id":          payment_row["id"],
                    "razorpay_payment_id": rzp_payment_id,
                    "razorpay_order_id":   rzp_order_id,
                    "amount":              float(amount_decimal),
                    "amount_paise":        amount_paise,
                    "payment_status":      "captured",
                    "merchant_id":         merchant_id,
                    "branch_id":           branch_id,
                    "source":              "webhook",
                    # ElevenLabs voice confirmation — FE plays voice_url
                    # directly via <audio src=...>; no auth required.
                    "should_play_voice":   bool(voice_url),
                    "voice_url":           voice_url or None,
                    "voice_amount_rupees": float(amount_decimal),
                    "voice_language":      "en",
                },
                branch_id=branch_id,
                restaurant_id=merchant_id,
                entity_id=payment_row.get("order_id"),
            )
        except Exception:  # noqa: BLE001
            logger.exception("rzp_webhook_ws_push_failed",
                             payment_id=payment_row["id"])

        # ── Auto Route split (net-of-Bittu-fee) ──────────────────────────
        # Money flows: Customer → Razorpay → Bittu master → (this transfer) →
        # Merchant's linked account. If the merchant has not finished Route
        # onboarding (no activated linked account), we silently skip and
        # the funds stay on the Bittu master account until manual payout.
        # This block is best-effort: it MUST NOT raise out of the webhook.
        try:
            from app.services.razorpay.route_service import rzp_route_service as _route
            from app.services.fee_service import fee_service

            linked = await _route.get_linked_account(merchant_id=merchant_id)
            # Razorpay V2 keeps account-level ``status`` at 'created' for the
            # entire happy-path lifetime — activation flows through the
            # *product*. Match the gate used by assert_settlement_ready /
            # get_active_linked_account_id: product activated AND account
            # not suspended.
            _eff = (linked or {}).get("effective_status")
            _prod = (linked or {}).get("route_product_status")
            _acc_status = (linked or {}).get("status")
            _is_ready = bool(linked) and _eff == "activated"
            if not _is_ready:
                logger.info(
                    "rzp_auto_split_skipped_no_linked_account",
                    merchant_id=merchant_id,
                    razorpay_payment_id=rzp_payment_id,
                    linked_status=_acc_status,
                    effective_status=_eff,
                    route_product_status=_prod,
                )
            else:
                # Bittu withholds 0.415% (0.3517% fee + GST); the merchant
                # transfer is gross - bittu_fee - estimated_rzp_charge.
                # The Razorpay charge is an *estimate* here (the actual fee
                # isn't known until settlement) and is trued-up later from
                # rzp_settlements/rzp_route_transfers — so the Bittu margin
                # stays exact at 0.415% regardless of estimate error.
                # ``fee_service.compute_fee`` is kept for audit metadata only.
                from app.services.razorpay.fee_policy import (
                    provisional_merchant_transfer_paise,
                )
                fee_payload = await fee_service.compute_fee(
                    merchant_id,
                    gross=amount_decimal,
                    payment_method=(payment_row.get("method") or entity.get("method") or "online"),
                    currency=(entity.get("currency") or "INR"),
                    record=False,
                )
                _method = (payment_row.get("method") or entity.get("method") or "online")
                net_paise, bittu_fee_paise_v, est_rzp_paise = (
                    provisional_merchant_transfer_paise(amount_paise, _method)
                )
                commission_paise = amount_paise - net_paise  # bittu_fee + est_rzp
                if net_paise < 100:
                    # Razorpay rejects transfers under ₹1.
                    logger.warning(
                        "rzp_auto_split_skipped_below_min",
                        merchant_id=merchant_id,
                        razorpay_payment_id=rzp_payment_id,
                        gross_paise=amount_paise,
                        merchant_share_paise=net_paise,
                    )
                else:
                    await _route.create_transfer(
                        merchant_id=merchant_id,
                        razorpay_payment_id=rzp_payment_id,
                        amount_paise=net_paise,
                        currency=(entity.get("currency") or "INR"),
                        notes={
                            "source":            "auto_split_on_capture",
                            "commission_paise":  str(commission_paise),
                            "merchant_share":    str(net_paise),
                            "bittu_fee_paise":   str(bittu_fee_paise_v),
                            "est_rzp_paise":     str(est_rzp_paise),
                            "rzp_gateway_fee":   str(fee_payload.get("fee") or 0),
                            "rzp_gateway_gst":   str(fee_payload.get("gst") or 0),
                            "internal_order_id": str(internal_order_id or ""),
                        },
                    )
                    logger.info(
                        "rzp_auto_split_ok",
                        merchant_id=merchant_id,
                        razorpay_payment_id=rzp_payment_id,
                        gross_paise=amount_paise,
                        commission_paise=commission_paise,
                        net_paise=net_paise,
                    )
        except Exception:  # noqa: BLE001
            logger.exception(
                "rzp_auto_split_failed",
                merchant_id=merchant_id,
                razorpay_payment_id=rzp_payment_id,
            )
    elif not merchant_id:
        logger.warning(
            "rzp_webhook_captured_orphan",
            razorpay_order_id=rzp_order_id,
            razorpay_payment_id=rzp_payment_id,
        )

    await _static_qr_webhook_sideeffect(_events.EVENT_PAYMENT_CAPTURED, envelope)
    return {"rzp_payment_uuid": payment_uuid}


async def _handle_payment_failed(envelope: dict, signature: Optional[str]) -> dict:
    entity = _entity(envelope, "payment")
    rzp_order_id = entity.get("order_id")
    ctx = await _resolve_order_context_by_rzp_order(rzp_order_id) or {}

    payment_uuid = await _upsert_rzp_payment(
        entity=entity, envelope=envelope, signature=signature,
        merchant_id=ctx.get("merchant_id"),
        branch_id=ctx.get("branch_id"),
        internal_order_id=ctx.get("internal_order_id"),
        rzp_order_uuid=ctx.get("rzp_order_uuid"),
        status="failed", captured=False,
    )
    payment_row = await _mark_payments_row(
        rzp_order_id=rzp_order_id,
        rzp_payment_id=entity.get("id"),
        new_status="failed",
        expected_current=("pending", "initiated"),
    )
    if payment_row:
        try:
            await emit_and_publish(DomainEvent(
                event_type=PAYMENT_FAILED,
                payload={
                    "payment_id":          payment_row["id"],
                    "razorpay_order_id":   rzp_order_id,
                    "razorpay_payment_id": entity.get("id"),
                    "error_code":          entity.get("error_code"),
                    "error_description":   entity.get("error_description"),
                    "source":              "webhook",
                },
            ))
        except Exception:  # noqa: BLE001
            logger.exception("rzp_webhook_event_emit_failed",
                             payment_id=payment_row["id"])
    await _static_qr_webhook_sideeffect(_events.EVENT_PAYMENT_FAILED, envelope)
    return {"rzp_payment_uuid": payment_uuid}


# ════════════════════════════════════════════════════════════════════════
# Order handler
# ════════════════════════════════════════════════════════════════════════

async def _handle_order_paid(envelope: dict, signature: Optional[str]) -> dict:
    """`order.paid` is informational — `payment.captured` does the real work."""
    order_entity = _entity(envelope, "order")
    payment_entity = _entity(envelope, "payment")
    rzp_order_id = order_entity.get("id")
    amount_paid = int(order_entity.get("amount_paid") or order_entity.get("amount") or 0)
    await _bump_rzp_order_paid(rzp_order_id=rzp_order_id, amount_paid_paise=amount_paid)
    # Forward to captured handler if a payment entity is present and we
    # haven't already processed it.
    if payment_entity.get("id"):
        await _handle_payment_captured(envelope, signature)
    return {}


# ════════════════════════════════════════════════════════════════════════
# QR handlers
# ════════════════════════════════════════════════════════════════════════

async def _upsert_rzp_qr(qr_entity: dict, envelope: dict) -> None:
    qr_id = qr_entity.get("id")
    if not qr_id:
        return
    raw = json.dumps(envelope)
    notes = json.dumps(qr_entity.get("notes") or {})
    close_by = qr_entity.get("close_by")
    closed_at = qr_entity.get("closed_at")
    async with get_service_connection() as conn:
        await conn.execute(
            """
            INSERT INTO rzp_qr_codes (
                qr_id, merchant_id, name, type, usage,
                fixed_amount, amount_paise, description,
                image_url, image_content,
                status, close_by, closed_at, close_reason,
                payments_amount_received_paise, payments_count_received,
                notes, raw_response
            ) VALUES (
                $1, COALESCE((
                    SELECT merchant_id FROM rzp_qr_codes WHERE qr_id = $1
                ), '00000000-0000-0000-0000-000000000000'::uuid),
                $2, $3, $4,
                $5, $6, $7,
                $8, $9,
                $10::rzp_qr_state, to_timestamp($11), to_timestamp($12), $13,
                $14, $15,
                $16::jsonb, $17::jsonb
            )
            ON CONFLICT (qr_id) DO UPDATE SET
                status                          = EXCLUDED.status,
                close_by                        = COALESCE(EXCLUDED.close_by, rzp_qr_codes.close_by),
                closed_at                       = COALESCE(EXCLUDED.closed_at, rzp_qr_codes.closed_at),
                close_reason                    = COALESCE(EXCLUDED.close_reason, rzp_qr_codes.close_reason),
                payments_amount_received_paise  = GREATEST(EXCLUDED.payments_amount_received_paise,
                                                           rzp_qr_codes.payments_amount_received_paise),
                payments_count_received         = GREATEST(EXCLUDED.payments_count_received,
                                                           rzp_qr_codes.payments_count_received),
                raw_response                    = EXCLUDED.raw_response,
                updated_at                      = NOW()
            """,
            qr_id,
            qr_entity.get("name"),
            qr_entity.get("type") or "upi_qr",
            qr_entity.get("usage") or "single_use",
            bool(qr_entity.get("fixed_amount", True)),
            qr_entity.get("payment_amount") or qr_entity.get("amount"),
            qr_entity.get("description"),
            qr_entity.get("image_url"),
            qr_entity.get("image_content"),
            (qr_entity.get("status") or "active"),
            close_by, closed_at, qr_entity.get("close_reason"),
            int(qr_entity.get("payments_amount_received") or 0),
            int(qr_entity.get("payments_count_received") or 0),
            notes, raw,
        )


async def _handle_qr_created(envelope: dict, signature: Optional[str]) -> dict:
    await _upsert_rzp_qr(_entity(envelope, "qr_code"), envelope)
    return {}


async def _handle_qr_closed(envelope: dict, signature: Optional[str]) -> dict:
    qr_entity = _entity(envelope, "qr_code")
    await _upsert_rzp_qr(qr_entity, envelope)

    # If this QR never collected a payment, flip the linked internal payment
    # from 'initiated' → 'expired' so abandoned QRs stop polluting "pending
    # payments" dashboards and never count as revenue.
    qr_id = qr_entity.get("id")
    received = int(qr_entity.get("payments_amount_received") or 0)
    if not qr_id or received > 0:
        return {}

    ctx = await _resolve_order_context_by_qr(qr_id)
    if not ctx or not ctx.get("internal_order_id"):
        return {}

    internal_order_id = ctx["internal_order_id"]
    merchant_id = ctx.get("merchant_id")
    branch_id = ctx.get("branch_id")

    async with get_service_connection() as conn:
        # Guarded transition: only flip rows still 'initiated' / 'pending'.
        # Never touch authorized / completed / refunded / failed / cancelled.
        row = await conn.fetchrow(
            """
            UPDATE payments
               SET status = 'expired',
                   updated_at = NOW()
             WHERE order_id = $1::uuid
               AND status IN ('initiated','pending')
            RETURNING id::text AS payment_id, amount, currency
            """,
            internal_order_id,
        )

    if row is None:
        return {}

    try:
        await emit_and_publish(DomainEvent(
            event_type=PAYMENT_EXPIRED,
            payload={
                "order_id": internal_order_id,
                "payment_id": row["payment_id"],
                "qr_id": qr_id,
                "amount": float(row["amount"] or 0),
                "currency": row["currency"] or "INR",
                "reason": qr_entity.get("close_reason") or "qr_closed",
            },
            restaurant_id=merchant_id,
            branch_id=branch_id,
        ))
    except Exception:
        logger.exception("rzp_qr_closed_emit_failed", qr_id=qr_id)

    logger.info(
        "rzp_payment_expired_on_qr_close",
        qr_id=qr_id,
        order_id=internal_order_id,
        payment_id=row["payment_id"],
    )
    return {"payment_id": row["payment_id"], "status": "expired"}


async def _handle_qr_credited(envelope: dict, signature: Optional[str]) -> dict:
    """A QR collected a payment — bump QR counters and run captured pipeline."""
    qr_entity = _entity(envelope, "qr_code")
    payment_entity = _entity(envelope, "payment")
    qr_id = qr_entity.get("id")

    # Refresh QR aggregates first (Razorpay sends current totals on the entity).
    await _upsert_rzp_qr(qr_entity, envelope)

    if not payment_entity.get("id"):
        logger.warning("rzp_webhook_qr_credited_no_payment", qr_id=qr_id)
        return {}

    # If the payment carries no order_id (QRs without the order-link feature),
    # patch it from rzp_qr_order_links so the captured handler can resolve
    # merchant context.
    if not payment_entity.get("order_id"):
        ctx = await _resolve_order_context_by_qr(qr_id)
        if ctx and ctx.get("razorpay_order_id"):
            payment_entity = dict(payment_entity)
            payment_entity["order_id"] = ctx["razorpay_order_id"]
            # Rewrite the envelope copy so downstream sees the patched entity.
            envelope = json.loads(json.dumps(envelope))
            envelope.setdefault("payload", {}).setdefault("payment", {})["entity"] = payment_entity

    await _handle_payment_captured(envelope, signature)
    return {"qr_id": qr_id}


# ════════════════════════════════════════════════════════════════════════
# Refund handlers — record into rzp_refunds; refund_service owns the ledger
# ════════════════════════════════════════════════════════════════════════

async def _upsert_rzp_refund(refund_entity: dict, envelope: dict, status: str) -> None:
    refund_id = refund_entity.get("id")
    if not refund_id:
        return
    rzp_payment_id = refund_entity.get("payment_id")
    raw = json.dumps(envelope)
    notes = json.dumps(refund_entity.get("notes") or {})
    acquirer = json.dumps(refund_entity.get("acquirer_data") or {}) if refund_entity.get("acquirer_data") else None

    # Resolve merchant_id via the parent payment.
    merchant_id: Optional[str] = None
    async with get_service_connection() as conn:
        if rzp_payment_id:
            row = await conn.fetchrow(
                """
                SELECT p.merchant_id::text AS merchant_id
                FROM rzp_payments_index i
                JOIN rzp_payments p ON p.id = i.payment_uuid
                WHERE i.razorpay_payment_id = $1
                LIMIT 1
                """,
                rzp_payment_id,
            )
            if row:
                merchant_id = row["merchant_id"]

        if not merchant_id:
            logger.warning("rzp_webhook_refund_orphan",
                           refund_id=refund_id, payment_id=rzp_payment_id)
            merchant_id = "00000000-0000-0000-0000-000000000000"

        processed_at = refund_entity.get("processed_at") or refund_entity.get("created_at")
        await conn.execute(
            """
            INSERT INTO rzp_refunds (
                refund_id, razorpay_payment_id, merchant_id,
                amount_paise, currency,
                speed_requested, speed_processed,
                status, reason, batch_id, acquirer_data,
                notes, raw_payload, processed_at
            ) VALUES (
                $1, $2, $3::uuid,
                $4, $5,
                $6, $7,
                $8::rzp_refund_state, $9, $10, $11::jsonb,
                $12::jsonb, $13::jsonb,
                CASE WHEN $14::bigint IS NULL THEN NULL
                     ELSE to_timestamp($14::bigint) END
            )
            ON CONFLICT (refund_id) DO UPDATE SET
                status          = EXCLUDED.status,
                speed_processed = COALESCE(EXCLUDED.speed_processed, rzp_refunds.speed_processed),
                acquirer_data   = COALESCE(EXCLUDED.acquirer_data, rzp_refunds.acquirer_data),
                processed_at    = COALESCE(EXCLUDED.processed_at, rzp_refunds.processed_at),
                raw_payload     = EXCLUDED.raw_payload,
                updated_at      = NOW()
            """,
            refund_id, rzp_payment_id, merchant_id,
            int(refund_entity.get("amount") or 0),
            refund_entity.get("currency") or "INR",
            refund_entity.get("speed_requested"),
            refund_entity.get("speed_processed"),
            status,
            refund_entity.get("notes", {}).get("reason") if isinstance(refund_entity.get("notes"), dict) else None,
            refund_entity.get("batch_id"),
            acquirer,
            notes, raw,
            processed_at,
        )

    if status == "processed" and rzp_payment_id:
        # Mirror to canonical payments table — full refund flips status; partial
        # leaves it (refund_service tracks partials via refunds rows).
        async with get_service_connection() as conn:
            await conn.execute(
                """
                UPDATE payments
                SET status     = 'refunded'::payment_status,
                    updated_at = NOW()
                WHERE razorpay_payment_id = $1
                  AND status::text IN ('completed', 'settled')
                """,
                rzp_payment_id,
            )
        try:
            await emit_and_publish(DomainEvent(
                event_type=PAYMENT_REFUNDED,
                payload={
                    "razorpay_refund_id":  refund_id,
                    "razorpay_payment_id": rzp_payment_id,
                    "amount_paise":        int(refund_entity.get("amount") or 0),
                    "source":              "webhook",
                },
            ))
        except Exception:  # noqa: BLE001
            logger.exception("rzp_webhook_refund_event_emit_failed", refund_id=refund_id)


async def _handle_refund_created(envelope: dict, signature: Optional[str]) -> dict:
    await _upsert_rzp_refund(_entity(envelope, "refund"), envelope, status="pending")
    await _maybe_transition_local_refund(envelope, "processing")
    return {}


async def _handle_refund_processed(envelope: dict, signature: Optional[str]) -> dict:
    entity = _entity(envelope, "refund")
    await _upsert_rzp_refund(entity, envelope, status="processed")
    await _maybe_transition_local_refund(envelope, "succeeded")
    # Phase 8: claw back the merchant's share of the refund from their Route
    # linked account so the Bittu master ledger stays whole. This is the
    # auto-reversal path; it is idempotent at the Razorpay layer via
    # `rzp_transfer_reverse:{transfer_id}:{amount_paise}`.
    await _maybe_reverse_route_transfer_for_refund(entity)
    return {}


async def _maybe_reverse_route_transfer_for_refund(refund_entity: dict) -> None:
    refund_id = refund_entity.get("id")
    rzp_payment_id = refund_entity.get("payment_id")
    refund_amount_paise = int(refund_entity.get("amount") or 0)
    if not refund_id or not rzp_payment_id or refund_amount_paise <= 0:
        return
    try:
        async with get_service_connection() as conn:
            # Locate the auto-split transfer that funded this payment to the
            # merchant. There can be multiple historical transfers (e.g.
            # split adjustments); pick the most recent non-reversed one.
            transfer_row = await conn.fetchrow(
                """
                SELECT transfer_id, merchant_id::text AS merchant_id,
                       amount_paise, status::text AS status
                FROM rzp_route_transfers
                WHERE razorpay_payment_id = $1
                  AND merchant_id <> '00000000-0000-0000-0000-000000000000'::uuid
                  AND status IN ('created', 'processed')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                rzp_payment_id,
            )
            # Compute the merchant's share of the refund. We mirror the
            # auto-split formula: merchant got (transfer.amount / payment.amount)
            # of the original capture, so they refund the same proportion.
            payment_row = await conn.fetchrow(
                "SELECT amount_paise FROM rzp_payments WHERE razorpay_payment_id = $1 "
                "ORDER BY created_at DESC LIMIT 1",
                rzp_payment_id,
            )
        if transfer_row is None or payment_row is None:
            logger.info(
                "rzp_auto_reverse_skipped_no_transfer",
                refund_id=refund_id,
                razorpay_payment_id=rzp_payment_id,
                has_transfer=transfer_row is not None,
                has_payment=payment_row is not None,
            )
            return

        payment_total = int(payment_row["amount_paise"] or 0)
        transfer_total = int(transfer_row["amount_paise"] or 0)
        if payment_total <= 0 or transfer_total <= 0:
            return
        # Proportional clawback, capped at the transfer amount.
        merchant_share_paise = min(
            transfer_total,
            (refund_amount_paise * transfer_total) // payment_total,
        )
        if merchant_share_paise <= 0:
            return

        from app.services.razorpay.route_service import rzp_route_service
        try:
            await rzp_route_service.reverse_transfer(
                merchant_id=transfer_row["merchant_id"],
                transfer_id=transfer_row["transfer_id"],
                amount_paise=merchant_share_paise,
                notes={
                    "source":     "auto_reverse_on_refund",
                    "refund_id":  refund_id,
                    "rzp_payment_id": rzp_payment_id,
                },
                refund_id=refund_id,
            )
            logger.info(
                "rzp_auto_reverse_ok",
                refund_id=refund_id,
                transfer_id=transfer_row["transfer_id"],
                merchant_share_paise=merchant_share_paise,
            )
        except ValueError:
            # Already terminal — idempotent no-op.
            logger.info(
                "rzp_auto_reverse_already_terminal",
                refund_id=refund_id,
                transfer_id=transfer_row["transfer_id"],
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "rzp_auto_reverse_failed",
            refund_id=refund_id,
            razorpay_payment_id=rzp_payment_id,
        )


async def _handle_refund_failed(envelope: dict, signature: Optional[str]) -> dict:
    await _upsert_rzp_refund(_entity(envelope, "refund"), envelope, status="failed")
    await _maybe_transition_local_refund(envelope, "failed")
    return {}


async def _maybe_transition_local_refund(envelope: dict, new_status: str) -> None:
    """
    If the refund row in our local `refunds` table can be located via
    `gateway_refund_id`, transition it. This is the path that fires the
    merchant_ledger DEBIT for refunds initiated through the merchant API
    once Razorpay confirms settlement. Out-of-band refunds (issued from
    the Razorpay dashboard) have no local row → no-op.
    """
    entity = _entity(envelope, "refund")
    refund_id = entity.get("id")
    if not refund_id:
        return
    # Resolve merchant from rzp_refunds (it was just upserted above).
    try:
        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                "SELECT merchant_id::text AS merchant_id FROM rzp_refunds WHERE refund_id = $1",
                refund_id,
            )
        if row is None:
            return
        from app.services.refund_service import refund_service
        failure_reason = None
        if new_status == "failed":
            failure_reason = (
                entity.get("notes", {}).get("failure_reason")
                if isinstance(entity.get("notes"), dict) else None
            ) or "razorpay_refund_failed"
        await refund_service.transition_by_gateway_id(
            merchant_id=row["merchant_id"],
            gateway_refund_id=refund_id,
            new_status=new_status,
            failure_reason=failure_reason,
        )
    except Exception:  # noqa: BLE001
        logger.exception("rzp_local_refund_transition_failed", refund_id=refund_id)


async def _handle_refund_speed_changed(envelope: dict, signature: Optional[str]) -> dict:
    # Razorpay sends the current state on the entity; preserve it.
    entity = _entity(envelope, "refund")
    current_status = entity.get("status") or "pending"
    if current_status not in ("pending", "processed", "failed"):
        current_status = "pending"
    await _upsert_rzp_refund(entity, envelope, status=current_status)
    return {}


# ════════════════════════════════════════════════════════════════════════
# Dispute handlers — record into rzp_disputes; dispute_service owns the ledger
# ════════════════════════════════════════════════════════════════════════

_DISPUTE_STATUS = {
    _events.EVENT_DISPUTE_CREATED:         "open",
    _events.EVENT_DISPUTE_UNDER_REVIEW:    "under_review",
    _events.EVENT_DISPUTE_ACTION_REQUIRED: "under_review",
    _events.EVENT_DISPUTE_WON:             "won",
    _events.EVENT_DISPUTE_LOST:            "lost",
    _events.EVENT_DISPUTE_CLOSED:          "closed",
}


async def _handle_dispute(envelope: dict, signature: Optional[str]) -> dict:
    entity = _entity(envelope, "dispute")
    dispute_id = entity.get("id")
    if not dispute_id:
        return {}
    rzp_payment_id = entity.get("payment_id")
    event_name = envelope.get("event") or ""
    new_status = _DISPUTE_STATUS.get(event_name, "open")

    raw = json.dumps(envelope)
    evidence = json.dumps(entity.get("evidence") or {})

    # Resolve merchant_id from the parent payment.
    merchant_id: Optional[str] = None
    async with get_service_connection() as conn:
        if rzp_payment_id:
            row = await conn.fetchrow(
                """
                SELECT p.merchant_id::text AS merchant_id
                FROM rzp_payments_index i
                JOIN rzp_payments p ON p.id = i.payment_uuid
                WHERE i.razorpay_payment_id = $1
                LIMIT 1
                """,
                rzp_payment_id,
            )
            if row:
                merchant_id = row["merchant_id"]
        if not merchant_id:
            logger.warning("rzp_webhook_dispute_orphan",
                           dispute_id=dispute_id, payment_id=rzp_payment_id)
            merchant_id = "00000000-0000-0000-0000-000000000000"

        deadline = entity.get("respond_by") or entity.get("deadline_at")
        await conn.execute(
            """
            INSERT INTO rzp_disputes (
                dispute_id, razorpay_payment_id, merchant_id,
                amount_paise, currency,
                reason_code, reason_description, phase,
                status, deadline_at,
                evidence, raw_payload
            ) VALUES (
                $1, $2, $3::uuid,
                $4, $5,
                $6, $7, $8,
                $9::rzp_dispute_state,
                CASE WHEN $10::bigint IS NULL THEN NULL
                     ELSE to_timestamp($10::bigint) END,
                $11::jsonb, $12::jsonb
            )
            ON CONFLICT (dispute_id) DO UPDATE SET
                status             = EXCLUDED.status,
                phase              = COALESCE(EXCLUDED.phase, rzp_disputes.phase),
                deadline_at        = COALESCE(EXCLUDED.deadline_at, rzp_disputes.deadline_at),
                reason_code        = COALESCE(EXCLUDED.reason_code, rzp_disputes.reason_code),
                reason_description = COALESCE(EXCLUDED.reason_description, rzp_disputes.reason_description),
                evidence           = EXCLUDED.evidence,
                raw_payload        = EXCLUDED.raw_payload,
                updated_at         = NOW()
            """,
            dispute_id, rzp_payment_id, merchant_id,
            int(entity.get("amount") or 0),
            entity.get("currency") or "INR",
            entity.get("reason_code"), entity.get("reason_description"),
            entity.get("phase"),
            new_status,
            deadline,
            evidence, raw,
        )

    # Drive local `disputes` FSM (best-effort — never raise from a webhook).
    try:
        from app.services.dispute_service import dispute_service
        await dispute_service.upsert_from_razorpay(
            rzp_entity=entity,
            merchant_id=merchant_id,
            razorpay_status_override=new_status,
        )
    except Exception:  # noqa: BLE001
        logger.exception("rzp_local_dispute_upsert_failed", dispute_id=dispute_id)
    return {"dispute_id": dispute_id, "status": new_status}


# ════════════════════════════════════════════════════════════════════════
# Settlement handler
# ════════════════════════════════════════════════════════════════════════

async def _handle_settlement_processed(envelope: dict, signature: Optional[str]) -> dict:
    entity = _entity(envelope, "settlement")
    settlement_id = entity.get("id")
    if not settlement_id:
        return {}

    # Phase 6: rzp_settlement_service handles merchant resolution via
    # rzp_route_accounts.linked_account_id and the UPSERT (with platform-UUID
    # backfill behaviour).
    merchant_id: Optional[str] = None
    try:
        from app.services.razorpay.settlement_service import rzp_settlement_service
        result = await rzp_settlement_service.upsert_from_razorpay(
            rzp_entity=entity, status_override="processed",
        )
        merchant_id = (result or {}).get("merchant_id")
    except Exception:  # noqa: BLE001
        logger.exception("rzp_settlement_upsert_failed", settlement_id=settlement_id)

    # Back-link this settlement onto its Route transfers so dashboards
    # (e.g. static-QR payments) flip to "settled" immediately instead of
    # waiting up to 12h for the drift-catcher poll. Best-effort.
    try:
        from app.services.razorpay.route_service import rzp_route_service
        await rzp_route_service.backfill_transfer_settlement_links(
            settlement_id=settlement_id, merchant_id=merchant_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "rzp_settlement_transfer_backfill_failed",
            settlement_id=settlement_id,
        )
    return {"settlement_id": settlement_id}


# ════════════════════════════════════════════════════════════════════════
# Record-only handlers (downtime / virtual_account / invoice)
# ════════════════════════════════════════════════════════════════════════

async def _record_only(envelope: dict, signature: Optional[str]) -> dict:
    """For events we acknowledge but don't yet act on (Phase >3 will fill in)."""
    return {"recorded": True}


# ════════════════════════════════════════════════════════════════════════
# Route transfers (Phase 7)
# ════════════════════════════════════════════════════════════════════════

async def _handle_transfer(envelope: dict, signature: Optional[str]) -> dict:
    entity = _entity(envelope, "transfer")
    transfer_id = entity.get("id")
    if not transfer_id:
        return {}

    event_name = (envelope or {}).get("event") or ""
    status_override: Optional[str] = None
    if event_name == "transfer.processed":
        status_override = "processed"
    elif event_name == "transfer.failed":
        status_override = "failed"

    try:
        from app.services.razorpay.route_service import rzp_route_service
        await rzp_route_service.upsert_transfer_from_razorpay(
            rzp_entity=entity, status_override=status_override,
        )
    except Exception:  # noqa: BLE001
        logger.exception("rzp_transfer_upsert_failed", transfer_id=transfer_id)
    return {"transfer_id": transfer_id, "status": status_override or entity.get("status")}


# ════════════════════════════════════════════════════════════════════════
# Smart Collect — virtual accounts (Phase 8)
# ════════════════════════════════════════════════════════════════════════

async def _handle_va_created(envelope: dict, signature: Optional[str]) -> dict:
    va_entity = _entity(envelope, "virtual_account")
    va_id = va_entity.get("id")
    if not va_id:
        return {}
    try:
        from app.services.razorpay.smart_collect_service import rzp_smart_collect_service
        await rzp_smart_collect_service.upsert_va_from_razorpay(rzp_entity=va_entity)
    except Exception:  # noqa: BLE001
        logger.exception("rzp_va_upsert_failed", virtual_account_id=va_id)
    return {"virtual_account_id": va_id}


async def _handle_va_closed(envelope: dict, signature: Optional[str]) -> dict:
    va_entity = _entity(envelope, "virtual_account")
    va_id = va_entity.get("id")
    if not va_id:
        return {}
    try:
        from app.services.razorpay.smart_collect_service import rzp_smart_collect_service
        await rzp_smart_collect_service.upsert_va_from_razorpay(rzp_entity=va_entity)
    except Exception:  # noqa: BLE001
        logger.exception("rzp_va_close_upsert_failed", virtual_account_id=va_id)
    return {"virtual_account_id": va_id, "status": "closed"}


async def _handle_va_credited(envelope: dict, signature: Optional[str]) -> dict:
    """
    Inbound bank-transfer / UPI credit landed on a virtual account.

    Razorpay envelope shape::

        payload.virtual_account.entity   -> the VA (with refreshed totals)
        payload.payment.entity           -> the inbound payment
        payload.bank_transfer.entity     -> NEFT/RTGS/IMPS metadata (when applicable)
        payload.upi.entity               -> UPI metadata (when applicable)

    The VA upsert refreshes amount_paid_paise; the txn upsert mirrors
    the inbound payment into rzp_smart_collect_txn for reconciliation.
    """
    va_entity = _entity(envelope, "virtual_account")
    payment_entity = _entity(envelope, "payment")
    bank_transfer_entity = _entity(envelope, "bank_transfer") or None
    upi_entity = _entity(envelope, "upi") or None
    va_id = va_entity.get("id")

    try:
        from app.services.razorpay.smart_collect_service import rzp_smart_collect_service
        if va_id:
            await rzp_smart_collect_service.upsert_va_from_razorpay(rzp_entity=va_entity)
        if payment_entity.get("id"):
            await rzp_smart_collect_service.upsert_txn_from_razorpay(
                payment_entity=payment_entity,
                bank_transfer_entity=bank_transfer_entity or None,
                upi_entity=upi_entity or None,
                va_entity=va_entity,
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "rzp_va_credited_failed",
            virtual_account_id=va_id,
            payment_id=payment_entity.get("id"),
        )
    return {
        "virtual_account_id": va_id,
        "payment_id": payment_entity.get("id"),
    }


# ════════════════════════════════════════════════════════════════════════
# Invoices (Phase 9)
# ════════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════
# Subscriptions (onboarding SaaS billing)
# ════════════════════════════════════════════════════════════════════════

async def _handle_subscription(envelope: dict, signature: Optional[str]) -> dict:
    """Mirror a subscription state transition into ``merchant_subscriptions``.

    Drives the onboarding payment gate: once a subscription is
    authenticated/active the merchant may proceed to settings + KYC. Never
    raises out of the webhook path (subscription_service swallows + logs).
    """
    event_name = (envelope or {}).get("event") or ""
    try:
        from app.services.subscription_service import subscription_service
        return await subscription_service.handle_subscription_webhook(
            event=event_name, envelope=envelope
        )
    except Exception:  # noqa: BLE001
        logger.exception("rzp_subscription_webhook_failed", event_name=event_name)
        return {"status": "failed", "event": event_name}


async def _handle_invoice(envelope: dict, signature: Optional[str]) -> dict:
    """
    Mirror an invoice state transition into ``rzp_invoices``.

    Razorpay envelope shape on ``invoice.paid`` / ``invoice.partially_paid``::

        payload.invoice.entity   -> the invoice (with refreshed amount_paid)
        payload.payment.entity   -> the payment that triggered the update
        payload.order.entity     -> the order linked to the invoice

    On ``invoice.expired`` only ``payload.invoice.entity`` is present.

    We DO NOT delegate to ``_handle_payment_captured`` here — the order
    associated with the invoice will fire its own ``payment.captured``
    webhook independently and that's the canonical credit path.
    """
    invoice_entity = _entity(envelope, "invoice")
    invoice_id = invoice_entity.get("id")
    if not invoice_id:
        return {}
    try:
        from app.services.razorpay.invoice_service import rzp_invoice_service
        await rzp_invoice_service.upsert_invoice_from_razorpay(
            rzp_entity=invoice_entity,
        )
    except Exception:  # noqa: BLE001
        logger.exception("rzp_invoice_upsert_failed", invoice_id=invoice_id)
    return {
        "invoice_id": invoice_id,
        "status": invoice_entity.get("status"),
    }


# ════════════════════════════════════════════════════════════════════════
# Handler registry
# ════════════════════════════════════════════════════════════════════════

_HANDLERS: dict[str, Callable[[dict, Optional[str]], Awaitable[dict]]] = {
    # payments
    _events.EVENT_PAYMENT_AUTHORIZED:       _handle_payment_authorized,
    _events.EVENT_PAYMENT_CAPTURED:         _handle_payment_captured,
    _events.EVENT_PAYMENT_FAILED:           _handle_payment_failed,
    # orders
    _events.EVENT_ORDER_PAID:               _handle_order_paid,
    # QR
    _events.EVENT_QR_CODE_CREATED:          _handle_qr_created,
    _events.EVENT_QR_CODE_CREDITED:         _handle_qr_credited,
    _events.EVENT_QR_CODE_CLOSED:           _handle_qr_closed,
    # refunds
    _events.EVENT_REFUND_CREATED:           _handle_refund_created,
    _events.EVENT_REFUND_PROCESSED:         _handle_refund_processed,
    _events.EVENT_REFUND_FAILED:            _handle_refund_failed,
    _events.EVENT_REFUND_SPEED_CHANGED:     _handle_refund_speed_changed,
    # disputes
    _events.EVENT_DISPUTE_CREATED:          _handle_dispute,
    _events.EVENT_DISPUTE_UNDER_REVIEW:     _handle_dispute,
    _events.EVENT_DISPUTE_ACTION_REQUIRED:  _handle_dispute,
    _events.EVENT_DISPUTE_WON:              _handle_dispute,
    _events.EVENT_DISPUTE_LOST:             _handle_dispute,
    _events.EVENT_DISPUTE_CLOSED:           _handle_dispute,
    # settlements
    _events.EVENT_SETTLEMENT_PROCESSED:     _handle_settlement_processed,
    # route transfers (Phase 7)
    _events.EVENT_TRANSFER_PROCESSED:       _handle_transfer,
    _events.EVENT_TRANSFER_FAILED:          _handle_transfer,
    # virtual accounts / smart collect (Phase 8)
    _events.EVENT_VA_CREATED:               _handle_va_created,
    _events.EVENT_VA_CREDITED:              _handle_va_credited,
    _events.EVENT_VA_CLOSED:                _handle_va_closed,
    # invoices (Phase 9)
    _events.EVENT_INVOICE_PAID:             _handle_invoice,
    _events.EVENT_INVOICE_PARTIALLY_PAID:   _handle_invoice,
    _events.EVENT_INVOICE_EXPIRED:          _handle_invoice,
    # subscriptions (onboarding SaaS billing)
    _events.EVENT_SUBSCRIPTION_AUTHENTICATED: _handle_subscription,
    _events.EVENT_SUBSCRIPTION_ACTIVATED:     _handle_subscription,
    _events.EVENT_SUBSCRIPTION_CHARGED:       _handle_subscription,
    _events.EVENT_SUBSCRIPTION_PENDING:       _handle_subscription,
    _events.EVENT_SUBSCRIPTION_HALTED:        _handle_subscription,
    _events.EVENT_SUBSCRIPTION_CANCELLED:     _handle_subscription,
    _events.EVENT_SUBSCRIPTION_COMPLETED:     _handle_subscription,
    _events.EVENT_SUBSCRIPTION_PAUSED:        _handle_subscription,
    _events.EVENT_SUBSCRIPTION_RESUMED:       _handle_subscription,
    _events.EVENT_SUBSCRIPTION_UPDATED:       _handle_subscription,
    # informational / record-only (Phase >3 fills in)
    _events.EVENT_DOWNTIME_STARTED:         _record_only,
    _events.EVENT_DOWNTIME_UPDATED:         _record_only,
    _events.EVENT_DOWNTIME_RESOLVED:        _record_only,
}


__all__ = ["dispatch_event"]
