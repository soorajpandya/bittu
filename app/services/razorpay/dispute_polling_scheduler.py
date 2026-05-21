"""
Razorpay dispute polling scheduler (Phase 5).

Webhooks are the primary path for dispute state changes, but they can be
missed (network blips, signature mismatch, replay-window drift). This loop
acts as a drift-catcher: every `interval_sec` we list disputes from Razorpay
that touched in the last `lookback_days` and reconcile each into the local
`disputes` table via `dispute_service.upsert_from_razorpay`.

Cross-merchant: we iterate every distinct `merchant_id` known to
`rzp_disputes` (chargebacks live there even before the local row exists),
plus all merchants that have at least one mirrored payment. The list is
bounded — at most a few dozen tenants in practice.

Idempotency: `upsert_from_razorpay` is fully idempotent — it locates the
local row through `rzp_disputes.internal_dispute_id` and only transitions
the FSM when the gateway state is strictly newer.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.core.database import get_service_connection
from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_INTERVAL_SEC = 6 * 3600          # 6 hours
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_PAGE_SIZE = 100
INITIAL_DELAY_SEC = 120                  # let API warm up


async def _candidate_merchants() -> list[str]:
    """Distinct merchants worth polling — anyone with a Razorpay footprint."""
    async with get_service_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT merchant_id::text AS merchant_id FROM rzp_disputes
            UNION
            SELECT merchant_id::text AS merchant_id FROM rzp_payments
            """
        )
    return [r["merchant_id"] for r in rows if r["merchant_id"]]


async def _poll_one_merchant(
    merchant_id: str, *, lookback_days: int, page_size: int,
) -> int:
    """List recent disputes for one merchant and reconcile each. Returns count."""
    from app.services.razorpay import disputes as rzp_disputes_api
    from app.services.dispute_service import dispute_service

    from_ts = int(
        (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()
    )
    try:
        resp = await rzp_disputes_api.list_disputes(
            count=page_size, skip=0, from_ts=from_ts, merchant_id=merchant_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "rzp_dispute_polling_list_failed",
            merchant_id=merchant_id, error=str(exc),
        )
        return 0

    items = (resp or {}).get("items") or []
    reconciled = 0
    for entity in items:
        try:
            await dispute_service.upsert_from_razorpay(
                rzp_entity=entity, merchant_id=merchant_id,
            )
            reconciled += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "rzp_dispute_polling_upsert_failed",
                merchant_id=merchant_id,
                rzp_dispute_id=entity.get("id"),
            )
    return reconciled


async def _scheduler_loop(interval_sec: int, lookback_days: int) -> None:
    logger.info(
        "rzp_dispute_polling_scheduler_started",
        interval_sec=interval_sec, lookback_days=lookback_days,
    )
    try:
        await asyncio.sleep(INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        raise

    while True:
        try:
            await run_once(lookback_days=lookback_days)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("rzp_dispute_polling_loop_error")

        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            raise


async def run_once(*, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict:
    """One-shot tick. Used by the loop AND by super-admin manual triggers."""
    started = datetime.now(timezone.utc)
    merchants = await _candidate_merchants()
    total = 0
    for mid in merchants:
        total += await _poll_one_merchant(
            mid, lookback_days=lookback_days, page_size=DEFAULT_PAGE_SIZE,
        )
    elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    result = {"merchants": len(merchants), "reconciled": total, "elapsed_ms": elapsed_ms}
    logger.info("rzp_dispute_polling_tick", **result)
    return result


def start_rzp_dispute_polling_scheduler(
    interval_sec: int = DEFAULT_INTERVAL_SEC,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> asyncio.Task:
    """Spawn the dispute polling loop. Caller must cancel/await on shutdown."""
    return asyncio.create_task(
        _scheduler_loop(interval_sec, lookback_days),
        name="rzp_dispute_polling_scheduler",
    )
