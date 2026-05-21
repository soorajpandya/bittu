"""
Razorpay Smart Collect polling scheduler (Phase 8).

Two passes per tick (default 1h):

  1. **Per-VA refresh** — for every ACTIVE row in ``rzp_smart_collect_va``
     we call ``fetch_virtual_account`` and re-mirror via
     ``rzp_smart_collect_service.upsert_va_from_razorpay``. Catches
     out-of-band closes / amount_paid bumps that may have missed a
     ``virtual_account.closed`` / ``credited`` webhook.

  2. **Recent payments** — for each active VA we list the last
     ``page_size`` inbound payments and re-mirror each into
     ``rzp_smart_collect_txn``. Drift-catcher for missed
     ``virtual_account.credited`` events.

Webhooks remain the primary path; this loop is a safety net.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.core.database import get_service_connection
from app.core.logging import get_logger
from app.services.razorpay.smart_collect_service import rzp_smart_collect_service

logger = get_logger(__name__)

DEFAULT_INTERVAL_SEC = 3600
DEFAULT_PAGE_SIZE = 50
INITIAL_DELAY_SEC = 180


async def _candidate_vas() -> list[tuple[str, str]]:
    """Returns list of (merchant_id, virtual_account_id) for active VAs."""
    async with get_service_connection() as conn:
        rows = await conn.fetch(
            "SELECT merchant_id::text AS merchant_id, virtual_account_id "
            "FROM rzp_smart_collect_va "
            "WHERE status = 'active'"
        )
    return [(r["merchant_id"], r["virtual_account_id"]) for r in rows]


async def _refresh_va(merchant_id: str, virtual_account_id: str) -> bool:
    try:
        await rzp_smart_collect_service.sync_virtual_account(
            merchant_id=merchant_id,
            virtual_account_id=virtual_account_id,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "rzp_va_polling_refresh_failed",
            merchant_id=merchant_id,
            virtual_account_id=virtual_account_id,
            error=str(exc),
        )
        return False


async def _pull_va_payments(
    merchant_id: str, virtual_account_id: str, *, page_size: int
) -> int:
    try:
        result = await rzp_smart_collect_service.sync_va_payments(
            merchant_id=merchant_id,
            virtual_account_id=virtual_account_id,
            count=page_size,
        )
        return int(result.get("count") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "rzp_va_polling_payments_failed",
            merchant_id=merchant_id,
            virtual_account_id=virtual_account_id,
            error=str(exc),
        )
        return 0


async def _scheduler_loop(interval_sec: int, page_size: int) -> None:
    logger.info(
        "rzp_smart_collect_polling_scheduler_started",
        interval_sec=interval_sec, page_size=page_size,
    )
    try:
        await asyncio.sleep(INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        raise

    while True:
        try:
            await run_once(page_size=page_size)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("rzp_smart_collect_polling_loop_error")

        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            raise


async def run_once(*, page_size: int = DEFAULT_PAGE_SIZE) -> dict:
    """One-shot tick. Used by the loop AND by super-admin manual triggers."""
    started = datetime.now(timezone.utc)
    vas = await _candidate_vas()
    va_refreshes = 0
    txns_total = 0
    for merchant_id, virtual_account_id in vas:
        if await _refresh_va(merchant_id, virtual_account_id):
            va_refreshes += 1
        txns_total += await _pull_va_payments(
            merchant_id, virtual_account_id, page_size=page_size,
        )
    elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    result = {
        "vas": len(vas),
        "va_refreshes": va_refreshes,
        "txns_upserted": txns_total,
        "elapsed_ms": elapsed_ms,
    }
    logger.info("rzp_smart_collect_polling_tick", **result)
    return result


def start_rzp_smart_collect_polling_scheduler(
    interval_sec: int = DEFAULT_INTERVAL_SEC,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> asyncio.Task:
    return asyncio.create_task(
        _scheduler_loop(interval_sec, page_size),
        name="rzp_smart_collect_polling_scheduler",
    )
