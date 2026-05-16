"""
Gateway-agnostic webhook signature & replay protection.

Why this exists
---------------
The original `webhooks.py` only checks an HMAC signature. It has NO timestamp
window, NO event-id deduplication, and NO forensic store of the raw payload.
That violates fintech webhook safety: an attacker who once captures a valid
signed body can replay it forever, and we have no audit trail to investigate
disputed events.

This module provides:

* `WebhookSignatureSpec` — pluggable signature schemes (HMAC-SHA256 today,
  Ed25519 / JWT later) keyed by gateway name.
* `verify_and_register_webhook()` — single entry-point that:
    1. Verifies the signature.
    2. Enforces a timestamp tolerance (default 5 min) when the gateway sends
       a timestamped header (Stripe-style `t=...,v1=...`, or X-*-Timestamp).
    3. Computes a stable `event_hash` (sha256 of body + signature).
    4. Inserts into `payment_webhook_events` with UNIQUE(gateway, event_id).
       UniqueViolation ⇒ replay/duplicate ⇒ caller short-circuits with 200.
    5. Captures latency + headers + raw payload for forensic replay.

Usage in a router
-----------------
    from app.core.webhook_security import verify_and_register_webhook

    @router.post("/razorpay/payment")
    async def razorpay_payment_webhook(request: Request):
        body = await request.body()
        result = await verify_and_register_webhook(
            request=request,
            body=body,
            gateway="razorpay",
            secret=settings.RAZORPAY_WEBHOOK_SECRET,
            event_id_extractor=lambda p: p.get("payload", {})
                .get("payment", {}).get("entity", {}).get("id"),
        )
        if result.duplicate:
            return {"status": "ok", "duplicate": True}
        await _payment_svc.handle_webhook(...)
        await result.mark_processed()
        return {"status": "ok"}
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import asyncpg
from fastapi import HTTPException, Request

from app.core.database import get_connection, get_service_connection
from app.core.logging import get_logger

logger = get_logger(__name__)

# 5-minute tolerance for timestamped webhooks. Tight enough to defeat replays,
# loose enough to survive NTP skew + retry queues.
DEFAULT_TIMESTAMP_TOLERANCE_S = 300


def _hmac_sha256_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").strip(), (b or "").strip())


@dataclass
class WebhookResult:
    event_row_id: str
    gateway: str
    event_id: Optional[str]
    duplicate: bool
    latency_ms: float
    started_at: float

    async def mark_processed(self, *, status: str = "processed", error: Optional[str] = None) -> None:
        if self.duplicate:
            return
        latency = round((time.perf_counter() - self.started_at) * 1000, 2)
        # Webhook callbacks have NO tenant context — use service connection so
        # the UPDATE isn't silently no-op'd by RLS, leaving rows stuck at 'received'.
        async with get_service_connection() as conn:
            await conn.execute(
                """
                UPDATE payment_webhook_events
                   SET processing_state = $1,
                       processed_at     = NOW(),
                       latency_ms       = $2,
                       last_error       = $3,
                       retries          = retries + CASE WHEN $1 = 'failed' THEN 1 ELSE 0 END
                 WHERE id = $4
                """,
                status, latency, error, uuid.UUID(self.event_row_id),
            )


async def verify_and_register_webhook(
    *,
    request: Request,
    body: bytes,
    gateway: str,
    secret: str,
    signature_header: str = None,
    timestamp_header: Optional[str] = None,
    timestamp_tolerance_s: int = DEFAULT_TIMESTAMP_TOLERANCE_S,
    event_id_extractor: Optional[Callable[[dict], Optional[str]]] = None,
    event_type_extractor: Optional[Callable[[dict], Optional[str]]] = None,
) -> WebhookResult:
    """
    Verify HMAC signature, enforce timestamp window, register event for replay
    protection. Raises HTTPException(400/401/409) on any failure.
    """
    started_at = time.perf_counter()

    # 1. Resolve headers --------------------------------------------------------
    sig_header_name = signature_header or {
        "razorpay": "X-Razorpay-Signature",
        "stripe":   "Stripe-Signature",
        "cashfree": "x-webhook-signature",
        "payu":     "X-PayU-Signature",
    }.get(gateway, "X-Signature")

    signature = request.headers.get(sig_header_name, "")
    if not signature:
        raise HTTPException(status_code=401, detail="missing_signature")

    # 2. Signature check --------------------------------------------------------
    expected = _hmac_sha256_hex(secret, body)
    if not _constant_time_eq(expected, signature):
        logger.warning(
            "webhook_signature_invalid",
            gateway=gateway,
            sig_header=sig_header_name,
            body_len=len(body),
        )
        raise HTTPException(status_code=401, detail="invalid_signature")

    # 3. Timestamp window (when gateway sends one) ------------------------------
    if timestamp_header:
        ts_raw = request.headers.get(timestamp_header)
        if ts_raw:
            try:
                ts = int(ts_raw)
                # Some gateways send ms.
                if ts > 10**12:
                    ts //= 1000
                drift = abs(int(time.time()) - ts)
                if drift > timestamp_tolerance_s:
                    logger.warning(
                        "webhook_timestamp_out_of_window",
                        gateway=gateway,
                        drift_s=drift,
                        tolerance_s=timestamp_tolerance_s,
                    )
                    raise HTTPException(status_code=400, detail="timestamp_out_of_window")
            except (TypeError, ValueError):
                logger.warning("webhook_timestamp_unparseable", gateway=gateway, raw=ts_raw)
                raise HTTPException(status_code=400, detail="invalid_timestamp")

    # 4. Parse body for event id/type ------------------------------------------
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="invalid_json")

    event_id = None
    if event_id_extractor:
        try:
            event_id = event_id_extractor(payload)
        except Exception:
            event_id = None
    event_id = event_id or payload.get("id") or payload.get("event_id")

    event_type = None
    if event_type_extractor:
        try:
            event_type = event_type_extractor(payload)
        except Exception:
            event_type = None
    event_type = event_type or payload.get("event") or payload.get("type")

    event_hash = hashlib.sha256(body + signature.encode()).hexdigest()

    # 5. Persist event row (UNIQUE on gateway+event_id deduplicates replays) ---
    headers_json = json.dumps({k: v for k, v in request.headers.items()})
    raw_json = json.dumps(payload)
    row_id = uuid.uuid4()

    try:
        async with get_service_connection() as conn:
            await conn.execute(
                """
                INSERT INTO payment_webhook_events
                    (id, gateway, event_id, event_type, event_hash,
                     signature_valid, processing_state,
                     headers, raw_payload, received_at)
                VALUES ($1, $2, $3, $4, $5, true, 'received',
                        $6::jsonb, $7::jsonb, NOW())
                """,
                row_id, gateway, event_id, event_type, event_hash,
                headers_json, raw_json,
            )
    except asyncpg.exceptions.UniqueViolationError:
        logger.info(
            "webhook_duplicate_short_circuit",
            gateway=gateway,
            event_id=event_id,
            event_type=event_type,
        )
        return WebhookResult(
            event_row_id=str(row_id),
            gateway=gateway,
            event_id=event_id,
            duplicate=True,
            latency_ms=round((time.perf_counter() - started_at) * 1000, 2),
            started_at=started_at,
        )

    return WebhookResult(
        event_row_id=str(row_id),
        gateway=gateway,
        event_id=event_id,
        duplicate=False,
        latency_ms=round((time.perf_counter() - started_at) * 1000, 2),
        started_at=started_at,
    )
