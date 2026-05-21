"""
Razorpay Route polling scheduler (Phase 7).

Two passes per tick (default 12h):

  1. **Per-merchant linked-account refresh** — for every row in
     ``rzp_route_accounts`` we call ``fetch_linked_account`` and re-mirror
     via ``rzp_route_service.upsert_linked_account_from_razorpay``. Catches
     KYC / activation transitions that don't fire a webhook (we don't
     subscribe to ``account.*`` per the original Phase 2 exclusion).

  2. **Recent transfers list** — for every merchant we list the last
     ``lookback_days`` of transfers and re-mirror each. Catches the rare
     case where ``transfer.processed`` / ``transfer.failed`` was missed.

Webhooks remain the primary path; this loop is a drift-catcher.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.core.database import get_service_connection
from app.core.logging import get_logger
from app.services.razorpay import route as route_api
from app.services.razorpay.route_service import rzp_route_service

logger = get_logger(__name__)

DEFAULT_INTERVAL_SEC = 12 * 3600
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_PAGE_SIZE = 100
INITIAL_DELAY_SEC = 240


async def _candidate_accounts() -> list[tuple[str, str]]:
    """Returns list of (merchant_id, linked_account_id)."""
    async with get_service_connection() as conn:
        rows = await conn.fetch(
            "SELECT merchant_id::text AS merchant_id, linked_account_id "
            "FROM rzp_route_accounts "
            "WHERE linked_account_id IS NOT NULL"
        )
    return [(r["merchant_id"], r["linked_account_id"]) for r in rows]


async def _refresh_account(merchant_id: str, linked_account_id: str) -> bool:
    try:
        rzp_resp = await route_api.fetch_linked_account(
            linked_account_id, merchant_id=merchant_id,
        )
        await rzp_route_service.upsert_linked_account_from_razorpay(
            rzp_entity=rzp_resp, merchant_id_override=merchant_id,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "rzp_route_polling_account_failed",
            merchant_id=merchant_id, linked_account_id=linked_account_id,
            error=str(exc),
        )
        return False


async def _pull_transfers(merchant_id: str, *, lookback_days: int, page_size: int) -> int:
    # Razorpay /v1/transfers list doesn't take a recipient filter cleanly,
    # so we page recent platform-wide transfers (scoped per-merchant via
    # the API call header) and let the service resolve recipients.
    # NOTE: list_transfers in route.py doesn't expose from_ts; we rely on
    # the default ordering (most-recent first) and page_size to bound.
    try:
        resp = await route_api.list_transfers(
            count=page_size, skip=0, merchant_id=merchant_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "rzp_route_polling_list_failed",
            merchant_id=merchant_id, error=str(exc),
        )
        return 0

    items = (resp or {}).get("items") or []
    cutoff_epoch = int(
        (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()
    )
    upserted = 0
    for entity in items:
        created_at = entity.get("created_at")
        if isinstance(created_at, (int, float)) and int(created_at) < cutoff_epoch:
            continue
        try:
            await rzp_route_service.upsert_transfer_from_razorpay(
                rzp_entity=entity, merchant_id_override=merchant_id,
            )
            upserted += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "rzp_route_polling_upsert_failed",
                merchant_id=merchant_id, transfer_id=entity.get("id"),
            )
    return upserted


async def _scheduler_loop(interval_sec: int, lookback_days: int) -> None:
    logger.info(
        "rzp_route_polling_scheduler_started",
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
            logger.exception("rzp_route_polling_loop_error")

        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            raise


async def run_once(*, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict:
    """One-shot tick. Used by the loop AND by super-admin manual triggers."""
    started = datetime.now(timezone.utc)
    accounts = await _candidate_accounts()
    account_refreshes = 0
    transfers_total = 0
    for merchant_id, linked_account_id in accounts:
        if await _refresh_account(merchant_id, linked_account_id):
            account_refreshes += 1
        transfers_total += await _pull_transfers(
            merchant_id,
            lookback_days=lookback_days,
            page_size=DEFAULT_PAGE_SIZE,
        )
    elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    result = {
        "accounts": len(accounts),
        "account_refreshes": account_refreshes,
        "transfers_upserted": transfers_total,
        "elapsed_ms": elapsed_ms,
    }
    logger.info("rzp_route_polling_tick", **result)
    return result


def start_rzp_route_polling_scheduler(
    interval_sec: int = DEFAULT_INTERVAL_SEC,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> asyncio.Task:
    return asyncio.create_task(
        _scheduler_loop(interval_sec, lookback_days),
        name="rzp_route_polling_scheduler",
    )
