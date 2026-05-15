"""
Razorpay Invoice polling scheduler (Phase 9).

Once per ``DEFAULT_INTERVAL_SEC`` (default 6h) we walk every active
merchant that has at least one invoice in a non-terminal state
(``draft`` / ``issued`` / ``partially_paid``) and:

  1. Fetch the latest invoice entity from Razorpay (per-invoice GET).
  2. Re-mirror via ``rzp_invoice_service.upsert_invoice_from_razorpay``.

Webhooks are still the canonical signal — this loop catches the rare
``invoice.partially_paid`` / ``invoice.expired`` event we may have
missed (network blip, restart between sign + ingest).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.core.database import get_service_connection
from app.core.logging import get_logger
from app.services.razorpay import invoices as inv_api
from app.services.razorpay.invoice_service import rzp_invoice_service

logger = get_logger(__name__)

DEFAULT_INTERVAL_SEC = 6 * 3600
INITIAL_DELAY_SEC = 180
MAX_INVOICES_PER_TICK = 500


async def _candidate_invoices() -> list[tuple[str, str]]:
    """Returns list of (merchant_id, invoice_id) for non-terminal invoices."""
    async with get_service_connection() as conn:
        rows = await conn.fetch(
            "SELECT merchant_id::text AS merchant_id, invoice_id "
            "FROM rzp_invoices "
            "WHERE status IN ('draft','issued','partially_paid') "
            "ORDER BY updated_at ASC NULLS FIRST "
            "LIMIT $1",
            MAX_INVOICES_PER_TICK,
        )
    return [(r["merchant_id"], r["invoice_id"]) for r in rows]


async def _refresh_invoice(merchant_id: str, invoice_id: str) -> bool:
    try:
        rzp_resp = await inv_api.fetch_invoice(
            invoice_id, merchant_id=merchant_id,
        )
        await rzp_invoice_service.upsert_invoice_from_razorpay(
            rzp_entity=rzp_resp,
            merchant_id_override=merchant_id,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "rzp_invoice_polling_refresh_failed",
            merchant_id=merchant_id,
            invoice_id=invoice_id,
            error=str(exc),
        )
        return False


async def _scheduler_loop(interval_sec: int) -> None:
    logger.info(
        "rzp_invoice_polling_scheduler_started",
        interval_sec=interval_sec,
    )
    try:
        await asyncio.sleep(INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        raise

    while True:
        started = datetime.now(timezone.utc)
        try:
            invoices = await _candidate_invoices()
            refreshed = 0
            for merchant_id, invoice_id in invoices:
                if await _refresh_invoice(merchant_id, invoice_id):
                    refreshed += 1

            logger.info(
                "rzp_invoice_polling_tick",
                candidates=len(invoices),
                refreshed=refreshed,
                elapsed_ms=int(
                    (datetime.now(timezone.utc) - started).total_seconds() * 1000
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("rzp_invoice_polling_loop_error")

        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            raise


def start_rzp_invoice_polling_scheduler(
    interval_sec: int = DEFAULT_INTERVAL_SEC,
) -> asyncio.Task:
    return asyncio.create_task(
        _scheduler_loop(interval_sec),
        name="rzp_invoice_polling_scheduler",
    )
