"""
Service wrapper around the immutable financial event store (migration 053).

Why this exists
---------------
Operational tables (orders, payments, settlements, refunds, disputes...) are
mutable. The platform's financial truth must NEVER mutate. Every money-affecting
change emits a hash-chained event into `financial_events` so:

  * settlement lifecycle can be replayed end-to-end
  * merchant balance history can be reconstructed without touching ops tables
  * regulators / forensic auditors get a tamper-evident chain per stream
  * the outbox pattern (Batch 5) consumes from this same stream for pub/sub

Usage
-----
    from app.services.financial_events_service import financial_events

    await financial_events.append(
        aggregate_type="payment",
        aggregate_id=payment_id,
        event_type="payment.captured",
        payload={"amount": "120.00", "gateway": "razorpay", "method": "upi"},
        actor_type="webhook",
        correlation_id=request.state.request_id,
    )

Stream semantics
----------------
Each (aggregate_type, aggregate_id) pair is its own monotonic stream. Versions
start at 1 and increment by 1. The DB function takes a per-stream advisory
lock so concurrent appends to the SAME stream serialise; appends to DIFFERENT
streams run fully in parallel.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Optional

from app.core.database import get_connection, get_service_connection
from app.core.logging import get_logger

logger = get_logger(__name__)

ALLOWED_AGGREGATE_TYPES = frozenset({
    "payment", "refund", "settlement", "escrow",
    "merchant_ledger", "merchant_liability", "fee",
    "dispute", "recon", "payout", "kyc",
})


class FinancialEventsService:
    async def append(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str | uuid.UUID,
        event_type: str,
        payload: dict,
        event_version: int = 1,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str | uuid.UUID] = None,
        actor_type: str = "system",
        actor_id: Optional[str | uuid.UUID] = None,
        occurred_at: Optional[datetime] = None,
        conn=None,
    ) -> dict:
        """
        Append a single event to its stream. Returns
        {event_id, stream_version, row_hash, prev_hash}.

        Always uses a SERVICE connection (RLS-bypass) because financial
        events span all merchants and must never be filtered by tenant.
        """
        if aggregate_type not in ALLOWED_AGGREGATE_TYPES:
            raise ValueError(f"unknown aggregate_type: {aggregate_type}")
        if not event_type or "." not in event_type:
            raise ValueError("event_type must be of form 'domain.action'")

        # asyncpg expects native types; serialize payload as jsonb at the boundary.
        payload_json = json.dumps(payload, default=str, sort_keys=True)
        sql = """
            SELECT fn_append_financial_event(
                $1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10
            ) AS result
        """
        params = (
            aggregate_type,
            uuid.UUID(str(aggregate_id)),
            event_type,
            payload_json,
            event_version,
            correlation_id,
            uuid.UUID(str(causation_id)) if causation_id else None,
            actor_type,
            uuid.UUID(str(actor_id)) if actor_id else None,
            occurred_at,
        )

        try:
            if conn is not None:
                row = await conn.fetchrow(sql, *params)
            else:
                async with get_service_connection() as c:
                    row = await c.fetchrow(sql, *params)
        except Exception:
            logger.exception(
                "financial_event_append_failed",
                aggregate_type=aggregate_type,
                aggregate_id=str(aggregate_id),
                event_type=event_type,
                correlation_id=correlation_id,
            )
            raise

        result = row["result"]
        if isinstance(result, str):
            result = json.loads(result)
        return result

    async def get_stream(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str | uuid.UUID,
        from_version: int = 1,
    ) -> list[dict]:
        async with get_service_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT id, stream_version, event_type, event_version,
                       payload, prev_hash, row_hash, correlation_id,
                       causation_id, actor_type, actor_id,
                       occurred_at, created_at
                  FROM financial_events
                 WHERE aggregate_type = $1
                   AND aggregate_id   = $2
                   AND stream_version >= $3
                 ORDER BY stream_version
                """,
                aggregate_type, uuid.UUID(str(aggregate_id)), from_version,
            )
        return [dict(r) for r in rows]

    async def verify_stream(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str | uuid.UUID,
    ) -> list[dict]:
        async with get_service_connection() as conn:
            rows = await conn.fetch(
                "SELECT * FROM fn_verify_financial_stream($1, $2)",
                aggregate_type, uuid.UUID(str(aggregate_id)),
            )
        return [dict(r) for r in rows]


financial_events = FinancialEventsService()
