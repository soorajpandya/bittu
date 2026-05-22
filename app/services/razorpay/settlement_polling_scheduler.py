"""
Razorpay settlement polling scheduler (Phase 6).

Two reconciliation passes per tick (default 24h):

  1. **Per-merchant settlement list** — for every merchant with a Razorpay
     linked account in `rzp_route_accounts`, list settlements from the last
     `lookback_days` and re-mirror via `rzp_settlement_service.upsert_from_razorpay`.

  2. **Recon report** — for every merchant, pull yesterday's combined recon
     report so each Bittu payment is mapped back to the settlement that
     swept it. Idempotent on `(settlement_id, razorpay_payment_id, type)`.

Webhooks are still the primary path; this loop is a drift-catcher for
missed/dropped events and for the recon report (which has no webhook).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.core.database import get_service_connection
from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_INTERVAL_SEC = 24 * 3600         # 1 day
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_PAGE_SIZE = 100
INITIAL_DELAY_SEC = 180                  # let API warm up


async def _candidate_merchants() -> list[tuple[str, str]]:
    """Return ``(merchant_id, linked_account_id)`` for every merchant whose
    Razorpay linked account is **activated**.

    Non-activated accounts (``created``, ``rejected``, ``suspended``,
    ``deleted``) are skipped because:
      * Razorpay rejects ``X-Razorpay-Account: acc_xxx`` requests for
        non-activated accounts with 400; and
      * they have no settlement data to mirror in the first place.
    """
    async with get_service_connection() as conn:
        rows = await conn.fetch(
            "SELECT merchant_id::text AS merchant_id, linked_account_id "
            "FROM rzp_route_accounts "
            "WHERE merchant_id IS NOT NULL "
            "  AND linked_account_id IS NOT NULL "
            "  AND status = 'activated'"
        )
    return [(r["merchant_id"], r["linked_account_id"]) for r in rows]


async def _pull_settlements_for_merchant(
    merchant_id: str, linked_account_id: str,
    *, lookback_days: int, page_size: int,
) -> int:
    from app.services.razorpay import settlements as rzp_settlements_api
    from app.services.razorpay.settlement_service import rzp_settlement_service

    from_ts = int(
        (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()
    )
    try:
        resp = await rzp_settlements_api.list_settlements(
            count=page_size, skip=0, from_ts=from_ts,
            merchant_id=merchant_id,
            account_id=linked_account_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "rzp_settlement_polling_list_failed",
            merchant_id=merchant_id, linked_account_id=linked_account_id,
            error=str(exc),
        )
        return 0

    items = (resp or {}).get("items") or []
    upserted = 0
    for entity in items:
        try:
            await rzp_settlement_service.upsert_from_razorpay(
                rzp_entity=entity, merchant_id_override=merchant_id,
            )
            upserted += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "rzp_settlement_polling_upsert_failed",
                merchant_id=merchant_id,
                linked_account_id=linked_account_id,
                settlement_id=entity.get("id"),
            )
    return upserted


async def _pull_recon_for_merchant(
    merchant_id: str, linked_account_id: str,
) -> dict:
    """Pull yesterday's recon report for a merchant's linked account."""
    from app.services.razorpay.settlement_service import rzp_settlement_service

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    try:
        return await rzp_settlement_service.fetch_recon_and_persist(
            year=yesterday.year, month=yesterday.month, day=yesterday.day,
            merchant_id=merchant_id,
            account_id=linked_account_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "rzp_settlement_polling_recon_failed",
            merchant_id=merchant_id, linked_account_id=linked_account_id,
            error=str(exc),
        )
        return {"seen": 0, "inserted": 0, "error": str(exc)}


async def _scheduler_loop(interval_sec: int, lookback_days: int) -> None:
    logger.info(
        "rzp_settlement_polling_scheduler_started",
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
            logger.exception("rzp_settlement_polling_loop_error")

        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            raise


async def run_once(*, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict:
    """One-shot tick. Used by the loop AND by super-admin manual triggers."""
    started = datetime.now(timezone.utc)
    merchants = await _candidate_merchants()
    settlements_total = 0
    recon_total = 0
    for mid, linked_account_id in merchants:
        settlements_total += await _pull_settlements_for_merchant(
            mid, linked_account_id,
            lookback_days=lookback_days, page_size=DEFAULT_PAGE_SIZE,
        )
        recon = await _pull_recon_for_merchant(mid, linked_account_id)
        recon_total += int(recon.get("inserted") or 0)
    elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    result = {
        "merchants": len(merchants),
        "settlements_upserted": settlements_total,
        "recon_rows_inserted": recon_total,
        "elapsed_ms": elapsed_ms,
    }
    logger.info("rzp_settlement_polling_tick", **result)
    return result


def start_rzp_settlement_polling_scheduler(
    interval_sec: int = DEFAULT_INTERVAL_SEC,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> asyncio.Task:
    """Spawn the settlement polling loop. Caller must cancel/await on shutdown."""
    return asyncio.create_task(
        _scheduler_loop(interval_sec, lookback_days),
        name="rzp_settlement_polling_scheduler",
    )
