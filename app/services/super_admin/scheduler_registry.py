"""
Scheduler registry — maps logical scheduler names to their `run_once()`
helpers so super-admin can trigger a single tick on demand. Every entry
also persists a row in `super_admin_scheduler_runs` for audit/history.

The auto-loop schedulers continue to run on their normal intervals; this
module is for manual reconciliation pokes (e.g. "I just changed Razorpay
keys, sync the settlements right now").
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from app.core.database import get_service_connection
from app.core.logging import get_logger
from app.services.razorpay import (
    dispute_polling_scheduler,
    invoice_polling_scheduler,
    qr_cleanup_scheduler,
    route_polling_scheduler,
    settlement_polling_scheduler,
    smart_collect_polling_scheduler,
)

logger = get_logger(__name__)


# Public name → (callable, default-kwargs, description, default interval sec)
SCHEDULERS: dict[str, dict[str, Any]] = {
    "rzp_route_polling": {
        "run":         route_polling_scheduler.run_once,
        "description": "Refresh linked accounts + recent transfers from Razorpay (drift catcher).",
        "interval_sec": route_polling_scheduler.DEFAULT_INTERVAL_SEC,
    },
    "rzp_settlement_polling": {
        "run":         settlement_polling_scheduler.run_once,
        "description": "Pull settlements + reconciliation rows from Razorpay.",
        "interval_sec": settlement_polling_scheduler.DEFAULT_INTERVAL_SEC,
    },
    "rzp_dispute_polling": {
        "run":         dispute_polling_scheduler.run_once,
        "description": "Reconcile disputes (chargebacks / lost / won) from Razorpay.",
        "interval_sec": dispute_polling_scheduler.DEFAULT_INTERVAL_SEC,
    },
    "rzp_smart_collect_polling": {
        "run":         smart_collect_polling_scheduler.run_once,
        "description": "Refresh virtual accounts + recent VA payments.",
        "interval_sec": smart_collect_polling_scheduler.DEFAULT_INTERVAL_SEC,
    },
    "rzp_invoice_polling": {
        "run":         invoice_polling_scheduler.run_once,
        "description": "Refresh non-terminal invoices from Razorpay.",
        "interval_sec": invoice_polling_scheduler.DEFAULT_INTERVAL_SEC,
    },
    "rzp_qr_cleanup": {
        "run":         qr_cleanup_scheduler.run_once,
        "description": "Close TTL-expired QR codes locally.",
        "interval_sec": qr_cleanup_scheduler.DEFAULT_INTERVAL_SEC,
    },
}


def list_schedulers() -> list[dict[str, Any]]:
    return [
        {
            "name":         name,
            "description":  meta["description"],
            "interval_sec": meta["interval_sec"],
        }
        for name, meta in sorted(SCHEDULERS.items())
    ]


async def list_recent_runs(
    *, scheduler_name: Optional[str] = None, limit: int = 50,
) -> list[dict[str, Any]]:
    async with get_service_connection() as conn:
        if scheduler_name:
            rows = await conn.fetch(
                """
                SELECT id, scheduler_name, triggered_by::text AS triggered_by,
                       triggered_by_email, started_at, finished_at, status,
                       result, error
                  FROM super_admin_scheduler_runs
                 WHERE scheduler_name = $1
                 ORDER BY started_at DESC
                 LIMIT $2
                """,
                scheduler_name, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, scheduler_name, triggered_by::text AS triggered_by,
                       triggered_by_email, started_at, finished_at, status,
                       result, error
                  FROM super_admin_scheduler_runs
                 ORDER BY started_at DESC
                 LIMIT $1
                """,
                limit,
            )
    return [dict(r) for r in rows]


async def trigger(
    name: str, *, triggered_by: str, triggered_by_email: Optional[str] = None,
) -> dict[str, Any]:
    meta = SCHEDULERS.get(name)
    if meta is None:
        raise LookupError(f"unknown scheduler: {name}")

    run_fn: Callable[[], Awaitable[dict]] = meta["run"]

    async with get_service_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO super_admin_scheduler_runs
                (scheduler_name, triggered_by, triggered_by_email, status)
            VALUES ($1, $2::uuid, $3, 'running')
            RETURNING id, started_at
            """,
            name, triggered_by, triggered_by_email,
        )
    run_id = int(row["id"])
    started_at = row["started_at"]
    logger.info("super_admin_scheduler_triggered",
                name=name, run_id=run_id, by=triggered_by)

    try:
        # Run with a generous safety timeout — schedulers should normally
        # finish in seconds, not minutes. A stuck pass shouldn't pin the
        # admin's request indefinitely.
        result = await asyncio.wait_for(run_fn(), timeout=600)
    except Exception as exc:  # noqa: BLE001
        async with get_service_connection() as conn:
            await conn.execute(
                """
                UPDATE super_admin_scheduler_runs
                   SET finished_at = now(),
                       status      = 'failed',
                       error       = $2
                 WHERE id = $1
                """,
                run_id, str(exc)[:1000],
            )
        logger.exception("super_admin_scheduler_failed",
                         name=name, run_id=run_id)
        raise

    async with get_service_connection() as conn:
        await conn.execute(
            """
            UPDATE super_admin_scheduler_runs
               SET finished_at = now(),
                   status      = 'success',
                   result      = $2::jsonb
             WHERE id = $1
            """,
            run_id, _to_jsonb(result),
        )
    return {
        "run_id":         run_id,
        "scheduler_name": name,
        "started_at":     started_at,
        "result":         result,
    }


def _to_jsonb(value: Any) -> str:
    import json
    return json.dumps(value, default=str)
