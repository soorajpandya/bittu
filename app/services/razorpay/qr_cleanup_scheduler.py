"""
Razorpay QR cleanup scheduler.

Background loop that closes QR codes whose `close_by` (TTL) has elapsed but
that Razorpay still reports as `active`. Without this, expired QRs linger in
the merchant's Razorpay account and may block reuse of the same internal
order ID for retries.

Strategy:
  * Every `interval_sec` (default 5 min), select up to N expired-but-active
    rows from `rzp_qr_codes` and call `POST /v1/payments/qr_codes/{id}/close`.
  * Each row is processed independently — a failure on one QR does not abort
    the batch.
  * The DB write is best-effort: if the API call succeeds we mark the row
    `closed`; if Razorpay reports the QR as already closed (HTTP 4xx with
    a known body) we still mark the row to avoid retry storms.
  * Cross-merchant: each row's `merchant_id` is forwarded to the client so the
    `X-Razorpay-Account` header is set when Route is in play.

Idempotency: closing a QR is naturally idempotent on Razorpay's side — repeated
closes on an already-closed QR return 4xx, which we swallow.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.core.database import get_service_connection
from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_INTERVAL_SEC = 300        # 5 minutes
DEFAULT_BATCH_LIMIT = 200
INITIAL_DELAY_SEC = 90            # let API warm up


async def _close_expired_batch(limit: int = DEFAULT_BATCH_LIMIT) -> int:
    """Close up to `limit` expired QRs. Returns the number of rows closed."""
    from app.services.razorpay import qr_codes as rzp_qr_api

    async with get_service_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT qr_id, merchant_id::text AS merchant_id
            FROM rzp_qr_codes
            WHERE status = 'active'::rzp_qr_state
              AND close_by IS NOT NULL
              AND close_by < NOW()
            ORDER BY close_by ASC
            LIMIT $1
            """,
            limit,
        )

    if not rows:
        return 0

    from app.services.razorpay.client import (
        RazorpayBadRequestError,
        RazorpayAPIError,
    )

    closed = 0
    for r in rows:
        qr_id = r["qr_id"]
        merchant_id = r["merchant_id"]
        try:
            await rzp_qr_api.close_qr(qr_id, merchant_id=merchant_id)
            ok = True
        except RazorpayBadRequestError as exc:
            # 4xx (non-429) means Razorpay refused the close — almost always
            # because the QR is already closed/expired/not-found on their side.
            # The row's close_by is in the past, so retrying will never help.
            # Mark local state closed to stop the retry storm.
            ok = True
            logger.info(
                "rzp_qr_cleanup_close_4xx_treating_as_closed",
                qr_id=qr_id, merchant_id=merchant_id,
                status=exc.status_code, error_code=exc.error_code,
            )
        except (RazorpayAPIError, Exception) as exc:  # noqa: BLE001
            # 5xx / network / transient — leave row active, will retry next tick.
            ok = False
            logger.warning(
                "rzp_qr_cleanup_close_transient_failure",
                qr_id=qr_id, merchant_id=merchant_id, error=str(exc),
            )

        if not ok:
            continue

        try:
            async with get_service_connection() as conn2:
                await conn2.execute(
                    """
                    UPDATE rzp_qr_codes
                    SET status        = 'expired'::rzp_qr_state,
                        closed_at     = COALESCE(closed_at, NOW()),
                        close_reason  = COALESCE(close_reason, 'ttl_expired'),
                        updated_at    = NOW()
                    WHERE qr_id = $1
                      AND status = 'active'::rzp_qr_state
                    """,
                    qr_id,
                )
            closed += 1
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "rzp_qr_cleanup_db_update_failed",
                qr_id=qr_id, error=str(exc),
            )

    return closed


async def _scheduler_loop(interval_sec: int) -> None:
    logger.info("rzp_qr_cleanup_scheduler_started", interval_sec=interval_sec)
    try:
        await asyncio.sleep(INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        raise

    while True:
        started = datetime.now(timezone.utc)
        try:
            n = await _close_expired_batch()
            if n:
                logger.info(
                    "rzp_qr_cleanup_tick",
                    closed=n,
                    elapsed_ms=int(
                        (datetime.now(timezone.utc) - started).total_seconds() * 1000
                    ),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("rzp_qr_cleanup_loop_error")
        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            raise


def start_rzp_qr_cleanup_scheduler(
    interval_sec: int = DEFAULT_INTERVAL_SEC,
) -> asyncio.Task:
    """Spawn the QR cleanup loop. Caller must `await task` on shutdown."""
    return asyncio.create_task(
        _scheduler_loop(interval_sec), name="rzp_qr_cleanup_scheduler",
    )
