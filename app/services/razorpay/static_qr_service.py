"""
Static QR Payment Module service.

Creates and manages Razorpay QR codes with ``usage='multiple_use'`` and
``fixed_amount=False`` — reusable per-merchant static QRs that customers
scan to pay any amount. Built additively on top of the existing
Razorpay infrastructure WITHOUT modifying the order-based payment intent
or route settlement flow.

Architecture mirrors the order-QR pipeline:
    1. Razorpay creates the QR (with Bittu-resolved merchant display name
       used as both ``name`` and ``description``).
    2. Razorpay's hosted ``image_url`` is downloaded and decoded to extract
       the actual UPI intent string (``upi://pay?...``).
    3. A Bittu-branded QR PNG is generated from the extracted UPI intent
       and returned as a ``data:image/png;base64,...`` URL.
    4. Razorpay remains the payment source-of-truth — webhooks update
       ``rzp_static_qr_payments`` via :func:`handle_webhook_payment_event`.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from fastapi import HTTPException

from app.core.database import get_service_connection
from app.core.events import DomainEvent, emit_and_publish
from app.core.logging import get_logger
from app.services.razorpay import qr_codes as rzp_qr_api

logger = get_logger(__name__)


_STATIC_QR_USAGE = "multiple_use"
_STATIC_QR_TYPE = "upi_qr"

# Realtime event types — kept distinct from the order-payment events so the
# frontend can subscribe specifically to static-QR activity.
EVENT_STATIC_QR_PAYMENT_CAPTURED = "static_qr.payment.captured"
EVENT_STATIC_QR_PAYMENT_FAILED   = "static_qr.payment.failed"
EVENT_STATIC_QR_PAYMENT_AUTHORIZED = "static_qr.payment.authorized"


# ════════════════════════════════════════════════════════════════════════
# Merchant resolution
# ════════════════════════════════════════════════════════════════════════

async def _resolve_merchant_display(
    merchant_id: str,
    *,
    fallback_name: Optional[str] = None,
) -> tuple[str, str]:
    """Resolve ``(display_name, linked_account_id)`` for a merchant.

    Priority:
        1. Razorpay linked-account ``legal_business_name``
        2. ``restaurants.name``
        3. caller-provided ``fallback_name``

    Raises 409 when the merchant has no active Route-enabled linked
    account — static QR creation requires Route settlement to credit the
    merchant directly.
    """
    if not merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id required")

    async with get_service_connection() as conn:
        route_row = await conn.fetchrow(
            """
            SELECT linked_account_id,
                   legal_business_name,
                   status::text          AS status,
                   route_product_status
            FROM rzp_route_accounts
            WHERE merchant_id = $1::uuid
            """,
            merchant_id,
        )
        restaurant_row = await conn.fetchrow(
            "SELECT name FROM restaurants WHERE id = $1::uuid",
            merchant_id,
        )

    if not route_row or not route_row["linked_account_id"]:
        raise HTTPException(
            status_code=409,
            detail="merchant_not_settlement_ready: linked account missing",
        )
    if (route_row["status"] or "").lower() == "suspended":
        raise HTTPException(
            status_code=409,
            detail="merchant_not_settlement_ready: linked account suspended",
        )
    if (route_row["route_product_status"] or "").lower() != "activated":
        raise HTTPException(
            status_code=409,
            detail=(
                "merchant_not_settlement_ready: route product not activated "
                f"(status={route_row['route_product_status']!r})"
            ),
        )

    display_name = (
        (route_row["legal_business_name"] or "").strip()
        or (restaurant_row["name"].strip() if restaurant_row and restaurant_row["name"] else "")
        or (fallback_name or "").strip()
    )
    if not display_name:
        raise HTTPException(
            status_code=409,
            detail="merchant_display_name_unavailable",
        )

    return display_name, route_row["linked_account_id"]


# ════════════════════════════════════════════════════════════════════════
# Persistence
# ════════════════════════════════════════════════════════════════════════

async def _fetch_active_row(merchant_id: str) -> Optional[dict]:
    async with get_service_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT id::text               AS id,
                   merchant_id::text      AS merchant_id,
                   linked_account_id,
                   razorpay_qr_id,
                   usage,
                   fixed_amount,
                   status,
                   merchant_display_name,
                   original_qr_image_url,
                   upi_intent,
                   bittu_qr_image,
                   notes,
                   created_at,
                   updated_at
            FROM rzp_static_qr_codes
            WHERE merchant_id = $1::uuid AND status = 'active'
            LIMIT 1
            """,
            merchant_id,
        )
    return dict(row) if row else None


def _row_to_response(row: dict) -> dict:
    return {
        "id": row["id"],
        "merchant_id": row["merchant_id"],
        "linked_account_id": row["linked_account_id"],
        "razorpay_qr_id": row["razorpay_qr_id"],
        "usage": row["usage"],
        "fixed_amount": row["fixed_amount"],
        "status": row["status"],
        "merchant_display_name": row["merchant_display_name"],
        "original_qr_image_url": row["original_qr_image_url"],
        "bittu_qr_image": row["bittu_qr_image"],
        "upi_intent": row["upi_intent"],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


# ════════════════════════════════════════════════════════════════════════
# Public API: create / get / regenerate
# ════════════════════════════════════════════════════════════════════════

async def get_active_static_qr(merchant_id: str) -> Optional[dict]:
    row = await _fetch_active_row(merchant_id)
    return _row_to_response(row) if row else None


async def create_static_qr(
    *,
    merchant_id: str,
    fallback_name: Optional[str] = None,
    extra_notes: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Create (or return existing) static QR for a merchant.

    Idempotent on the active row — calling this repeatedly returns the
    same QR until it is explicitly regenerated.
    """
    existing = await _fetch_active_row(merchant_id)
    if existing:
        return _row_to_response(existing)

    display_name, linked_account_id = await _resolve_merchant_display(
        merchant_id, fallback_name=fallback_name,
    )

    notes: dict[str, Any] = {
        "merchant_id": str(merchant_id),
        "linked_account_id": linked_account_id,
        # Marker used by the webhook side-effect to identify static-QR
        # payments without having to query the static QR table on every
        # incoming payment event.
        "bittu_static_qr": "1",
    }
    if extra_notes:
        for k, v in extra_notes.items():
            notes.setdefault(str(k), str(v))

    qr_resp = await rzp_qr_api.create_qr(
        name=display_name[:30],
        amount_paise=None,
        description=display_name[:100],
        fixed_amount=False,
        usage=_STATIC_QR_USAGE,
        qr_type=_STATIC_QR_TYPE,
        notes=notes,
        idempotency_key=f"static_qr:{merchant_id}",
        merchant_id=str(merchant_id),
    )

    razorpay_qr_id = qr_resp.get("id")
    if not razorpay_qr_id:
        raise HTTPException(
            status_code=502,
            detail="razorpay_static_qr_create_failed: no qr id in response",
        )

    image_url = qr_resp.get("image_url")
    image_content = qr_resp.get("image_content")

    upi_intent: Optional[str] = None
    if image_content and str(image_content).startswith("upi://pay"):
        upi_intent = str(image_content)
    elif image_url:
        resolved, source = await rzp_qr_api.resolve_upi_intent_for_qr(
            upi_intent=image_content,
            image_url=image_url,
            qr_id=razorpay_qr_id,
            merchant_id=str(merchant_id),
            fixed_amount=False,
            payment_amount_paise=None,
            payer_name=display_name,
        )
        if resolved:
            upi_intent = resolved
            logger.info(
                "static_qr_upi_intent_extracted",
                merchant_id=str(merchant_id),
                qr_id=razorpay_qr_id,
                source=source,
            )

    bittu_qr_image: Optional[str] = None
    if upi_intent:
        try:
            bittu_qr_image = rzp_qr_api.generate_qr_data_url_from_upi_intent(upi_intent)
        except Exception:
            logger.exception(
                "static_qr_bittu_generation_failed",
                merchant_id=str(merchant_id),
                qr_id=razorpay_qr_id,
            )

    async with get_service_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO rzp_static_qr_codes (
                merchant_id, linked_account_id, razorpay_qr_id,
                usage, fixed_amount, status, merchant_display_name,
                original_qr_image_url, upi_intent, bittu_qr_image,
                notes, raw_response
            ) VALUES (
                $1::uuid, $2, $3,
                $4, $5, 'active', $6,
                $7, $8, $9,
                $10::jsonb, $11::jsonb
            )
            ON CONFLICT (razorpay_qr_id) DO UPDATE SET
                merchant_display_name = EXCLUDED.merchant_display_name,
                original_qr_image_url = EXCLUDED.original_qr_image_url,
                upi_intent            = COALESCE(EXCLUDED.upi_intent, rzp_static_qr_codes.upi_intent),
                bittu_qr_image        = COALESCE(EXCLUDED.bittu_qr_image, rzp_static_qr_codes.bittu_qr_image),
                raw_response          = EXCLUDED.raw_response,
                updated_at            = NOW()
            RETURNING id::text               AS id,
                      merchant_id::text      AS merchant_id,
                      linked_account_id,
                      razorpay_qr_id,
                      usage,
                      fixed_amount,
                      status,
                      merchant_display_name,
                      original_qr_image_url,
                      upi_intent,
                      bittu_qr_image,
                      notes,
                      created_at,
                      updated_at
            """,
            str(merchant_id),
            linked_account_id,
            razorpay_qr_id,
            _STATIC_QR_USAGE,
            False,
            display_name,
            image_url,
            upi_intent,
            bittu_qr_image,
            json.dumps(notes),
            json.dumps(qr_resp, default=str),
        )

    return _row_to_response(dict(row))


async def regenerate_static_qr(
    *,
    merchant_id: str,
    fallback_name: Optional[str] = None,
) -> dict:
    """Close any active static QR for the merchant and mint a new one."""
    existing = await _fetch_active_row(merchant_id)
    if existing:
        try:
            await rzp_qr_api.close_qr(
                existing["razorpay_qr_id"], merchant_id=str(merchant_id),
            )
        except Exception:
            logger.exception(
                "static_qr_close_failed",
                merchant_id=str(merchant_id),
                qr_id=existing["razorpay_qr_id"],
            )
        async with get_service_connection() as conn:
            await conn.execute(
                """
                UPDATE rzp_static_qr_codes
                SET status = 'closed',
                    closed_at = NOW(),
                    updated_at = NOW()
                WHERE id = $1::uuid
                """,
                existing["id"],
            )

    return await create_static_qr(
        merchant_id=merchant_id, fallback_name=fallback_name,
    )


async def close_static_qr(merchant_id: str) -> dict:
    existing = await _fetch_active_row(merchant_id)
    if not existing:
        raise HTTPException(status_code=404, detail="no_active_static_qr")
    try:
        await rzp_qr_api.close_qr(
            existing["razorpay_qr_id"], merchant_id=str(merchant_id),
        )
    except Exception:
        logger.exception(
            "static_qr_close_failed",
            merchant_id=str(merchant_id),
            qr_id=existing["razorpay_qr_id"],
        )
    async with get_service_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE rzp_static_qr_codes
            SET status = 'closed',
                closed_at = NOW(),
                updated_at = NOW()
            WHERE id = $1::uuid
            RETURNING id::text               AS id,
                      merchant_id::text      AS merchant_id,
                      linked_account_id,
                      razorpay_qr_id,
                      usage,
                      fixed_amount,
                      status,
                      merchant_display_name,
                      original_qr_image_url,
                      upi_intent,
                      bittu_qr_image,
                      notes,
                      created_at,
                      updated_at
            """,
            existing["id"],
        )
    return _row_to_response(dict(row))


# ════════════════════════════════════════════════════════════════════════
# Payments listing
# ════════════════════════════════════════════════════════════════════════

async def list_static_qr_payments(
    *,
    merchant_id: str,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    # WHERE conditions apply to the static-QR payments table (alias ``p``).
    where = ["p.merchant_id = $1::uuid"]
    args: list[Any] = [merchant_id]
    if status:
        args.append(status)
        where.append(f"p.status = ${len(args)}")
    where_sql = " AND ".join(where)

    args.extend([int(limit), int(offset)])
    # JOINs:
    #   t  → latest Route transfer for the same razorpay_payment_id (95% auto-split)
    #   rs → the linked-account settlement that rolled this transfer in, looked
    #        up by the transfer's own ``recipient_settlement_id``. This is the
    #        canonical "settled into the linked account" signal — it is exactly
    #        what the Razorpay Route → Transfers dashboard renders as
    #        "Settlement Status: Settled".
    #   s  → fallback: the merchant's bank-payout settlement that rolled this
    #        payment in (legacy / non-Route primary-account flow). For Route
    #        transfers this never populates, so relying on it alone left every
    #        Route payment stuck on "pending" even after Razorpay settled it.
    # ``settlement_status`` is normalised to the labels shown on the Razorpay
    # Route dashboard so the FE can render it as-is:
    #   not_applicable | pending | settled
    rows_sql = f"""
        SELECT p.id::text                  AS id,
               p.razorpay_payment_id,
               p.razorpay_qr_id,
               p.merchant_id::text         AS merchant_id,
               p.linked_account_id,
               p.amount_paise,
               p.currency,
               p.status,
               p.payment_method,
               p.vpa,
               p.payer_email,
               p.payer_contact,
               p.failure_code,
               p.failure_reason,
               p.captured_at,
               p.created_at,
               p.updated_at,
               t.transfer_id,
               t.amount_paise               AS transfer_amount_paise,
               t.status::text               AS transfer_status,
               t.processed_at               AS transfer_processed_at,
               t.recipient_settlement_id    AS recipient_settlement_id,
               COALESCE(rs.settlement_id, s.settlement_id)   AS settlement_id,
               COALESCE(rs.status::text, s.status::text)     AS settlement_state_raw,
               COALESCE(rs.utr, s.utr)                       AS settlement_utr,
               COALESCE(rs.settled_at, s.settled_at)         AS settled_at,
               CASE
                   WHEN t.transfer_id IS NULL THEN 'not_applicable'
                   WHEN t.status::text IN ('failed', 'reversed') THEN 'not_applicable'
                   -- Razorpay has settled this transfer into the linked
                   -- account (dashboard shows "Settled"). This holds even when
                   -- our bank-payout mirror (s) has no matching row yet.
                   WHEN t.recipient_settlement_id IS NOT NULL THEN 'settled'
                   WHEN rs.status::text = 'processed' THEN 'settled'
                   WHEN s.status::text = 'processed' THEN 'settled'
                   WHEN t.status::text = 'processed' THEN 'pending'
                   ELSE 'not_applicable'
               END                          AS settlement_status
        FROM rzp_static_qr_payments p
        LEFT JOIN LATERAL (
            SELECT transfer_id, amount_paise, status, processed_at,
                   recipient_settlement_id
            FROM rzp_route_transfers
            WHERE razorpay_payment_id = p.razorpay_payment_id
              AND merchant_id        = p.merchant_id
            ORDER BY created_at DESC
            LIMIT 1
        ) t ON TRUE
        LEFT JOIN LATERAL (
            SELECT s3.settlement_id, s3.status, s3.utr, s3.settled_at
            FROM rzp_settlements s3
            WHERE s3.settlement_id = t.recipient_settlement_id
            LIMIT 1
        ) rs ON TRUE
        LEFT JOIN LATERAL (
            SELECT s2.settlement_id, s2.status, s2.utr, s2.settled_at
            FROM rzp_settlement_payments sp
            JOIN rzp_settlements s2 ON s2.settlement_id = sp.settlement_id
            WHERE sp.razorpay_payment_id = p.razorpay_payment_id
              AND sp.merchant_id        = p.merchant_id
            ORDER BY s2.settled_at DESC NULLS LAST, s2.created_at DESC
            LIMIT 1
        ) s ON TRUE
        WHERE {where_sql}
        ORDER BY p.created_at DESC
        LIMIT ${len(args) - 1} OFFSET ${len(args)}
    """
    count_sql = (
        f"SELECT COUNT(*) AS n FROM rzp_static_qr_payments p WHERE {where_sql}"
    )

    async with get_service_connection() as conn:
        rows = await conn.fetch(rows_sql, *args)
        count_row = await conn.fetchrow(count_sql, *args[: len(args) - 2])

    _ts_keys = ("created_at", "updated_at", "captured_at",
                "transfer_processed_at", "settled_at")

    def _serialise(r: dict) -> dict:
        out = {k: v for k, v in r.items() if k not in _ts_keys}
        for k in _ts_keys:
            v = r.get(k)
            out[k] = v.isoformat() if v else None
        return out

    return {
        "items": [_serialise(dict(r)) for r in rows],
        "total": int(count_row["n"]) if count_row else 0,
        "limit": int(limit),
        "offset": int(offset),
    }


# ════════════════════════════════════════════════════════════════════════
# Webhook side-effect — invoked from webhook_dispatcher
# ════════════════════════════════════════════════════════════════════════

def _epoch_to_ts_sql(epoch: Optional[int]) -> Optional[str]:
    """Helper: convert epoch seconds to ISO timestamp string for SQL."""
    if not epoch:
        return None
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except Exception:
        return None


async def _lookup_static_qr_by_razorpay_id(
    razorpay_qr_id: str,
) -> Optional[dict]:
    async with get_service_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT merchant_id::text     AS merchant_id,
                   linked_account_id,
                   razorpay_qr_id,
                   merchant_display_name
            FROM rzp_static_qr_codes
            WHERE razorpay_qr_id = $1
            ORDER BY status = 'active' DESC, created_at DESC
            LIMIT 1
            """,
            razorpay_qr_id,
        )
    return dict(row) if row else None


def _resolve_status(event_name: str, entity_status: Optional[str]) -> Optional[str]:
    """Map a webhook event + entity status to our local enum."""
    s = (entity_status or "").lower()
    if s in ("captured", "authorized", "failed", "refunded"):
        return s
    if event_name.endswith(".captured"):
        return "captured"
    if event_name.endswith(".authorized"):
        return "authorized"
    if event_name.endswith(".failed"):
        return "failed"
    return None


async def handle_webhook_payment_event(
    envelope: dict,
    *,
    event_name: str,
) -> Optional[dict]:
    """Side-effect invoked from ``webhook_dispatcher`` for every
    ``payment.*`` event. Returns ``None`` if the payment is not tied to a
    static QR; otherwise upserts the payment into ``rzp_static_qr_payments``
    and publishes a realtime event.
    """
    payload = (envelope or {}).get("payload") or {}
    payment_entity = (((payload.get("payment") or {})).get("entity")) or {}
    if not payment_entity:
        return None

    notes = payment_entity.get("notes") or {}
    if not isinstance(notes, dict):
        notes = {}

    # Resolve the static QR record via:
    #   1. our marker note `bittu_static_qr=1` (cheapest)
    #   2. echoed `razorpay_qr_id` / `qr_code` field on the payment entity
    razorpay_qr_id: Optional[str] = (
        payment_entity.get("qr_code")
        or payment_entity.get("qr_id")
        or notes.get("razorpay_qr_id")
    )

    static_qr_row: Optional[dict] = None
    if notes.get("bittu_static_qr") in ("1", 1, True, "true", "True") or razorpay_qr_id:
        if razorpay_qr_id:
            static_qr_row = await _lookup_static_qr_by_razorpay_id(razorpay_qr_id)
        if not static_qr_row and notes.get("merchant_id"):
            # Last-ditch: marker note present but no qr_id echoed — find the
            # merchant's active static QR.
            row = await _fetch_active_row(str(notes.get("merchant_id")))
            if row:
                static_qr_row = {
                    "merchant_id": row["merchant_id"],
                    "linked_account_id": row["linked_account_id"],
                    "razorpay_qr_id": row["razorpay_qr_id"],
                    "merchant_display_name": row["merchant_display_name"],
                }

    if not static_qr_row:
        return None

    status = _resolve_status(event_name, payment_entity.get("status"))
    if not status:
        return None

    razorpay_payment_id = payment_entity.get("id")
    if not razorpay_payment_id:
        return None

    amount_paise = int(payment_entity.get("amount") or 0)
    currency = (payment_entity.get("currency") or "INR")[:3]
    payment_method = payment_entity.get("method")
    vpa = payment_entity.get("vpa")
    payer_email = payment_entity.get("email")
    payer_contact = payment_entity.get("contact")
    failure_code = payment_entity.get("error_code")
    failure_reason = (
        payment_entity.get("error_description")
        or payment_entity.get("error_reason")
    )
    captured_at = _epoch_to_ts_sql(payment_entity.get("captured_at"))

    async with get_service_connection() as conn:
        await conn.execute(
            """
            INSERT INTO rzp_static_qr_payments (
                razorpay_payment_id, razorpay_qr_id, merchant_id, linked_account_id,
                amount_paise, currency, status, payment_method, vpa,
                payer_email, payer_contact, failure_code, failure_reason,
                raw_payload, captured_at
            ) VALUES (
                $1, $2, $3::uuid, $4,
                $5, $6, $7, $8, $9,
                $10, $11, $12, $13,
                $14::jsonb,
                CASE WHEN $15::text IS NULL THEN NULL ELSE $15::timestamptz END
            )
            ON CONFLICT (razorpay_payment_id) DO UPDATE SET
                status         = EXCLUDED.status,
                payment_method = COALESCE(EXCLUDED.payment_method, rzp_static_qr_payments.payment_method),
                vpa            = COALESCE(EXCLUDED.vpa,            rzp_static_qr_payments.vpa),
                payer_email    = COALESCE(EXCLUDED.payer_email,    rzp_static_qr_payments.payer_email),
                payer_contact  = COALESCE(EXCLUDED.payer_contact,  rzp_static_qr_payments.payer_contact),
                failure_code   = COALESCE(EXCLUDED.failure_code,   rzp_static_qr_payments.failure_code),
                failure_reason = COALESCE(EXCLUDED.failure_reason, rzp_static_qr_payments.failure_reason),
                raw_payload    = EXCLUDED.raw_payload,
                captured_at    = COALESCE(EXCLUDED.captured_at,    rzp_static_qr_payments.captured_at),
                updated_at     = NOW()
            """,
            razorpay_payment_id,
            static_qr_row["razorpay_qr_id"],
            static_qr_row["merchant_id"],
            static_qr_row.get("linked_account_id"),
            amount_paise,
            currency,
            status,
            payment_method,
            vpa,
            payer_email,
            payer_contact,
            failure_code,
            failure_reason,
            json.dumps(envelope, default=str),
            captured_at,
        )

    # ── Route auto-split (captured only) ──────────────────────────────
    # Mirrors the order-flow split in
    # webhook_dispatcher._handle_payment_captured: 95% merchant share is
    # routed to the linked account and 5% is retained as platform
    # commission. ``create_transfer`` is idempotent on
    # ``rzp_transfer:{merchant_id}:{razorpay_payment_id}:{amount}`` so
    # webhook replays cannot double-pay. Razorpay rejects transfers under
    # ₹1 (100 paise) — skip those captures silently.
    transfer_result: Optional[dict] = None
    if status == "captured" and amount_paise > 0:
        merchant_share_paise = (amount_paise * 95) // 100
        commission_paise = amount_paise - merchant_share_paise
        if merchant_share_paise < 100:
            logger.warning(
                "static_qr_auto_split_skipped_below_min",
                merchant_id=static_qr_row["merchant_id"],
                razorpay_payment_id=razorpay_payment_id,
                gross_paise=amount_paise,
                merchant_share_paise=merchant_share_paise,
            )
        else:
            try:
                from app.services.razorpay.route_service import (
                    rzp_route_service as _route,
                )
                transfer_result = await _route.create_transfer(
                    merchant_id=static_qr_row["merchant_id"],
                    razorpay_payment_id=razorpay_payment_id,
                    amount_paise=int(merchant_share_paise),
                    currency=currency,
                    notes={
                        "source":            "static_qr_auto_split_on_capture",
                        "razorpay_qr_id":    static_qr_row["razorpay_qr_id"],
                        "linked_account_id": static_qr_row.get("linked_account_id") or "",
                        "merchant_id":       static_qr_row["merchant_id"],
                        "gross_paise":       str(amount_paise),
                        "commission_paise":  str(commission_paise),
                        "merchant_share":    str(merchant_share_paise),
                    },
                )
                logger.info(
                    "static_qr_auto_split_ok",
                    merchant_id=static_qr_row["merchant_id"],
                    razorpay_payment_id=razorpay_payment_id,
                    gross_paise=amount_paise,
                    commission_paise=commission_paise,
                    net_paise=merchant_share_paise,
                    transfer_count=len((transfer_result or {}).get("transfers") or []),
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "static_qr_auto_split_failed",
                    merchant_id=static_qr_row["merchant_id"],
                    razorpay_payment_id=razorpay_payment_id,
                    gross_paise=amount_paise,
                )

    # Realtime fan-out.
    if status == "captured":
        event_type = EVENT_STATIC_QR_PAYMENT_CAPTURED
    elif status == "failed":
        event_type = EVENT_STATIC_QR_PAYMENT_FAILED
    elif status == "authorized":
        event_type = EVENT_STATIC_QR_PAYMENT_AUTHORIZED
    else:
        event_type = f"static_qr.payment.{status}"

    # Pre-generate the ElevenLabs payment-confirmation MP3 so the FE can
    # play it via <audio src="voice_url"> with no auth header / extra fetch.
    voice_url = ""
    voice_amount_rupees = None
    if status == "captured" and amount_paise:
        try:
            from app.services.elevenlabs_service import ElevenLabsService
            voice_amount_rupees = round(int(amount_paise) / 100.0, 2)
            voice_url = await ElevenLabsService().ensure_payment_voice_file(
                token=razorpay_payment_id,
                amount=voice_amount_rupees,
                language="en",
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "static_qr_voice_prepare_failed",
                razorpay_payment_id=razorpay_payment_id,
            )
            voice_url = ""

    try:
        await emit_and_publish(DomainEvent(
            event_type=event_type,
            payload={
                "type": "static_qr_payment",
                "status": status,
                "merchant_id": static_qr_row["merchant_id"],
                "linked_account_id": static_qr_row.get("linked_account_id"),
                "razorpay_qr_id": static_qr_row["razorpay_qr_id"],
                "razorpay_payment_id": razorpay_payment_id,
                "amount_paise": amount_paise,
                "currency": currency,
                "payment_method": payment_method,
                "vpa": vpa,
                "merchant_display_name": static_qr_row.get("merchant_display_name"),
                "failure_code": failure_code,
                "failure_reason": failure_reason,
                "captured_at": captured_at,
                # Frontend contract: auto-print bill only on `captured`.
                "should_print_bill": status == "captured",
                # Frontend contract: play ElevenLabs voice confirmation only
                # on `captured`. FE just does `new Audio(voice_url).play()` —
                # the MP3 is pre-generated server-side and served publicly
                # (token-gated by the unguessable razorpay_payment_id).
                "should_play_voice": status == "captured" and bool(voice_url),
                "voice_url": voice_url or None,
                "voice_amount_rupees": voice_amount_rupees,
                "voice_language": "en",
            },
            restaurant_id=static_qr_row["merchant_id"],
        ))
    except Exception:
        logger.exception(
            "static_qr_realtime_publish_failed",
            merchant_id=static_qr_row["merchant_id"],
            razorpay_payment_id=razorpay_payment_id,
            status=status,
        )

    return {
        "razorpay_payment_id": razorpay_payment_id,
        "razorpay_qr_id": static_qr_row["razorpay_qr_id"],
        "merchant_id": static_qr_row["merchant_id"],
        "status": status,
    }


__all__ = [
    "create_static_qr",
    "get_active_static_qr",
    "regenerate_static_qr",
    "close_static_qr",
    "list_static_qr_payments",
    "handle_webhook_payment_event",
    "EVENT_STATIC_QR_PAYMENT_CAPTURED",
    "EVENT_STATIC_QR_PAYMENT_FAILED",
    "EVENT_STATIC_QR_PAYMENT_AUTHORIZED",
]
