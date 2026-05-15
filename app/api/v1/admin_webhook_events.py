"""
Payment webhook events (admin) — read-only monitoring of gateway callbacks.

Prefix:   /admin/payments/webhooks
Audience: platform admins.

Source of truth: `payment_webhook_events` (partitioned, append-only).
The cockpit's Health page polls `/failures` to surface broken integrations.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import require_platform_admin
from app.core.database import get_connection

router = APIRouter(
    prefix="/admin/payments/webhooks",
    tags=["Payments (Admin)"],
)


def _row_to_failure(r) -> dict:
    """Normalize a payment_webhook_events row to the cockpit shape."""
    return {
        "id":          str(r["id"]),
        "gateway":     r["gateway"],
        "event_id":    r["event_id"],
        "event_type":  r["event_type"],
        "status":      r["processing_state"],
        "attempts":    r["retries"],
        "last_error":  r["last_error"],
        "occurred_at": r["received_at"],
        # `payload` intentionally omitted from list responses; pull via /{id}.
    }


@router.get("/failures")
async def list_webhook_failures(
    gateway: Optional[str]      = Query(None, description="razorpay|cashfree|..."),
    since:   Optional[datetime] = Query(
        None, description="Default: now() − 24h. Inclusive lower bound."
    ),
    limit:   int                = Query(50, ge=1, le=500),
    offset:  int                = Query(0, ge=0),
    _ = Depends(require_platform_admin()),
):
    """
    Webhook callbacks that did not reach `processed`. Includes both
    explicit `failed` rows and rows still stuck in `received` / `processing`
    older than 5 minutes (likely worker crash / DLQ).
    """
    if since is None:
        since = datetime.now(tz=timezone.utc) - timedelta(hours=24)

    where = ["received_at >= $1"]
    args:  list = [since]
    if gateway:
        args.append(gateway)
        where.append(f"gateway = ${len(args)}")

    # "Failure" = explicit failed OR stuck in non-terminal state >5 min.
    args.append(timedelta(minutes=5))
    stuck_idx = len(args)
    where.append(
        f"(processing_state = 'failed' "
        f" OR (processing_state IN ('received','processing') "
        f"     AND received_at < now() - ${stuck_idx}))"
    )
    where_sql = "WHERE " + " AND ".join(where)

    sql = f"""
        SELECT id, gateway, event_id, event_type, processing_state,
               retries, last_error, received_at
          FROM payment_webhook_events
          {where_sql}
         ORDER BY received_at DESC
         LIMIT ${len(args)+1} OFFSET ${len(args)+2}
    """
    count_sql = f"SELECT count(*) FROM payment_webhook_events {where_sql}"

    async with get_connection() as conn:
        rows  = await conn.fetch(sql, *args, limit, offset)
        total = await conn.fetchval(count_sql, *args)

    return {
        "items":  [_row_to_failure(r) for r in rows],
        "limit":  limit,
        "offset": offset,
        "total":  total,
        "since":  since,
    }


@router.get("/failures/{event_id}")
async def get_webhook_event(
    event_id: str,
    _ = Depends(require_platform_admin()),
):
    """Full row including raw payload + headers, for triage."""
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, gateway, event_id, event_type, processing_state,
                   retries, last_error, signature_valid, latency_ms,
                   headers, raw_payload, received_at, processed_at
              FROM payment_webhook_events
             WHERE id = $1::uuid
             ORDER BY received_at DESC
             LIMIT 1
            """,
            event_id,
        )
    if not row:
        raise HTTPException(404, "Webhook event not found")
    out = dict(row)
    out["id"] = str(out["id"])
    return out
