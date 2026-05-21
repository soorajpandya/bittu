"""
Razorpay reconciliation scheduler (Phase 9).

Runs `rzp_reconciliation_service.run_daily_reconciliation` once every
`interval_sec` (default 24h). The first tick is delayed by INITIAL_DELAY_SEC
so we don't slam the DB at boot.

Webhooks + the per-domain polling loops remain the primary integrity
mechanisms; this is the cross-domain integrity audit on top of them.
"""
from __future__ import annotations

import asyncio

from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_INTERVAL_SEC = 24 * 3600          # 1 day
INITIAL_DELAY_SEC = 300                   # 5 minutes after boot


async def _scheduler_loop(interval_sec: int) -> None:
    logger.info(
        "rzp_recon_scheduler_started",
        interval_sec=interval_sec,
    )
    try:
        await asyncio.sleep(INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        raise

    from app.services.razorpay.reconciliation import rzp_reconciliation_service

    while True:
        try:
            await rzp_reconciliation_service.run_daily_reconciliation(
                triggered_by="scheduler",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("rzp_recon_scheduler_tick_failed")

        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            raise


def start_rzp_reconciliation_scheduler(
    interval_sec: int = DEFAULT_INTERVAL_SEC,
) -> asyncio.Task:
    """Spawn the reconciliation scheduler loop. Caller must cancel/await on shutdown."""
    return asyncio.create_task(
        _scheduler_loop(interval_sec),
        name="rzp_reconciliation_scheduler",
    )
