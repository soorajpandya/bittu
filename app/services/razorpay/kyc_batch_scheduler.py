"""
Razorpay KYC batch scheduler.

Wakes up at every 30-minute slot boundary (00 / 30 past the hour, UTC) and
calls ``rzp_kyc_batch_service.generate_batch_for_slot()``. The service
itself is idempotent — re-running the same slot returns the existing row.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.core.logging import get_logger
from app.services.razorpay.kyc_batch_service import (
    current_slot,
    next_slot,
    rzp_kyc_batch_service,
)

logger = get_logger(__name__)

# Small jitter so we always run *after* the slot boundary, not exactly on it.
SLOT_JITTER_SEC = 5


def _seconds_until_next_slot() -> float:
    now = datetime.now(timezone.utc)
    target = next_slot(now)
    return max(1.0, (target - now).total_seconds() + SLOT_JITTER_SEC)


async def _scheduler_loop() -> None:
    logger.info("rzp_kyc_batch_scheduler_started")
    # Initial run — catch the slot we're in right now (covers restarts mid-slot).
    try:
        await run_once()
    except Exception:
        logger.exception("rzp_kyc_batch_initial_run_failed")

    while True:
        delay = _seconds_until_next_slot()
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("rzp_kyc_batch_scheduler_tick_failed")


async def run_once() -> dict:
    slot = current_slot()
    started = datetime.now(timezone.utc)
    result = await rzp_kyc_batch_service.generate_batch_for_slot(slot)
    elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    logger.info(
        "rzp_kyc_batch_tick",
        slot_at=slot.isoformat(),
        batch_no=result.get("batch_no"),
        record_count=result.get("record_count", 0),
        elapsed_ms=elapsed_ms,
    )
    # Reconcile any linked accounts Razorpay has since activated so the
    # merchant's app flips pending -> activated without a manual admin
    # click. Best-effort: never let a reconcile failure break the tick.
    try:
        recon = await rzp_kyc_batch_service.reconcile_pending_accounts()
        if recon.get("candidates"):
            logger.info("rzp_kyc_reconcile_tick", **recon)
    except Exception:
        logger.exception("rzp_kyc_reconcile_tick_failed")
    return result


def start_rzp_kyc_batch_scheduler() -> asyncio.Task:
    return asyncio.create_task(_scheduler_loop(), name="rzp_kyc_batch_scheduler")
