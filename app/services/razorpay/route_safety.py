"""Serialize Route mutations and retain ambiguous outcomes across process restarts."""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager

from app.core.database import get_service_connection
from app.core.exceptions import ValidationError, ConflictError, NotFoundError
from app.services.razorpay.client import RazorpayAPIError


@asynccontextmanager
async def payment_lock(payment_id: str):
    # Session lock: the intent must commit BEFORE the external HTTP operation.
    async with get_service_connection() as conn:
        key = "route:" + payment_id
        await conn.execute("SELECT pg_advisory_lock(hashtextextended($1, 0))", key)
        try:
            yield conn
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", key)


async def guarded_mutation(conn, *, key: str, resource_key: str, body: dict, send):
    """Call only while holding payment_lock, AFTER gateway reconciliation.

    Unknown outcomes require reconciliation or operator review; absence in one
    list response is not proof that a timed-out money movement never happened.
    """
    if await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM rzp_route_operations WHERE resource_key=$1 "
        "AND operation_key<>$2 AND state='unknown')", resource_key, key,
    ):
        raise RuntimeError("Earlier Route operation has an unknown outcome; reconcile before another mutation")
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    row = await conn.fetchrow("SELECT * FROM rzp_route_operations WHERE operation_key=$1", key)
    if row:
        if row["request_hash"] != digest:
            raise ValueError("Route operation key reused with different request")
        if row["state"] == "succeeded":
            value = row["response"]
            return json.loads(value) if isinstance(value, str) else value
        if row["state"] == "unknown":
            raise RuntimeError("Route outcome unknown: reconcile at Razorpay before retrying " + key)
    claimed = await conn.fetchval(
        "INSERT INTO rzp_route_operations(operation_key,request_hash,resource_key,state) VALUES($1,$2,$3,'unknown') "
        "ON CONFLICT(operation_key) DO UPDATE SET state='unknown',updated_at=now() "
        "WHERE rzp_route_operations.request_hash=EXCLUDED.request_hash "
        "AND rzp_route_operations.resource_key=EXCLUDED.resource_key AND rzp_route_operations.state='rejected' "
        "RETURNING operation_key", key, digest, resource_key,
    )
    if not claimed:
        raise ValueError("Route operation was already claimed or key was reused")
    try:
        result = await send()
    except (ValidationError, ConflictError, NotFoundError):
        await conn.execute("UPDATE rzp_route_operations SET state='rejected',updated_at=now() WHERE operation_key=$1", key)
        raise
    except RazorpayAPIError as exc:
        if 400 <= exc.status_code < 500 and exc.status_code not in {408, 409, 425, 429}:
            await conn.execute("UPDATE rzp_route_operations SET state='rejected',updated_at=now() WHERE operation_key=$1", key)
        raise
    await conn.execute(
        "UPDATE rzp_route_operations SET state='succeeded',response=$2::jsonb,updated_at=now() WHERE operation_key=$1",
        key, json.dumps(result, default=str),
    )
    return result
