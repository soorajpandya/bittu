"""
Forensic-safe structured audit logger.

Why this exists
---------------
Previously, services wrote audit rows with `str(payload)` and cast to ::jsonb.
That is INVALID JSON (single quotes, bare True/False, datetime reprs etc.) and
silently corrupted the audit trail. This module provides ONE place to write
audit events that is:

* JSON-safe (uses json.dumps with a default that handles UUID/Decimal/datetime)
* schema-stable (every event has the same envelope shape)
* tenant-stamped (restaurant_id + branch_id wherever available)
* request-correlated (request_id + correlation_id propagated from middleware)
* append-only at the DB layer (enforced by triggers in migration 049)

Usage
-----
    from app.core.audit_logger import audit_event

    await audit_event(
        action="payment.captured",
        actor=user,                      # UserContext or None for system events
        entity_type="payment",
        entity_id=str(payment_id),
        payload={"amount": amount, "gateway": "razorpay"},
        request=request,                 # optional FastAPI Request
    )

Domains
-------
The `domain` field encodes WHICH api surface produced the event:
    platform | merchant | branch | internal | system | public
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from app.core.database import get_connection
from app.core.logging import get_logger

logger = get_logger(__name__)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        # Stringify decimals to avoid float precision loss in audit logs.
        return str(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"audit_logger: unserializable type {type(obj).__name__}")


def safe_dumps(payload: Any) -> str:
    """Serialize an arbitrary payload to JSON for audit storage."""
    if payload is None:
        return "null"
    try:
        return json.dumps(payload, default=_json_default, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        # Last-resort fallback: never lose an audit event.
        return json.dumps({"_serialization_error": str(exc), "_repr": repr(payload)[:2000]})


def _extract_request_meta(request) -> dict:
    if request is None:
        return {}
    headers = getattr(request, "headers", {}) or {}
    state = getattr(request, "state", None)
    request_id = getattr(state, "request_id", None) if state else None
    correlation_id = (
        headers.get("x-correlation-id")
        or headers.get("x-request-id")
        or request_id
    )
    client = getattr(request, "client", None)
    ip = headers.get("x-forwarded-for", "").split(",")[0].strip() or (client.host if client else None)
    return {
        "request_id": request_id,
        "correlation_id": correlation_id,
        "client_ip": ip,
        "user_agent": headers.get("user-agent"),
    }


async def audit_event(
    *,
    action: str,
    entity_type: str,
    entity_id: Optional[str] = None,
    payload: Optional[dict] = None,
    actor=None,                # UserContext | None
    request=None,              # fastapi.Request | None
    domain: str = "merchant",
    old_values: Optional[dict] = None,
    new_values: Optional[dict] = None,
    conn=None,                 # optional asyncpg connection for in-tx writes
) -> None:
    """
    Write a single forensic audit row.

    Never raises — audit failures must NEVER fail the business operation.
    All exceptions are caught and logged via structlog.
    """
    meta = _extract_request_meta(request)
    actor_id = getattr(actor, "user_id", None) if actor else None
    restaurant_id = getattr(actor, "restaurant_id", None) if actor else None
    branch_id = getattr(actor, "branch_id", None) if actor else None
    role = getattr(actor, "role", None) if actor else None

    envelope = {
        "domain": domain,
        "action": action,
        "actor": {
            "user_id": str(actor_id) if actor_id else None,
            "role": role,
            "restaurant_id": str(restaurant_id) if restaurant_id else None,
            "branch_id": str(branch_id) if branch_id else None,
        },
        "request": meta,
        "entity": {"type": entity_type, "id": entity_id},
        "payload": payload or {},
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    new_data_json = safe_dumps({**envelope, "new_values": new_values} if new_values else envelope)
    old_data_json = safe_dumps(old_values) if old_values else None

    sql = """
        INSERT INTO audit_log
            (restaurant_id, user_id, action, entity_type, entity_id,
             new_data, old_data)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
    """
    params = (
        restaurant_id, actor_id, action, entity_type,
        str(entity_id) if entity_id else None,
        new_data_json, old_data_json,
    )

    try:
        if conn is not None:
            await conn.execute(sql, *params)
        else:
            async with get_connection() as c:
                await c.execute(sql, *params)
    except Exception:
        # Never fail the caller. Just emit a structured log line so ops can alert.
        logger.exception(
            "audit_event_write_failed",
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            domain=domain,
        )
