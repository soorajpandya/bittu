"""
Audit service — Phase 6 (append-only, hash-chained audit log).

Every sensitive action across the platform should be recorded here so that
admin investigations can reconstruct what happened, and so that any tamper
attempt on the database is detectable via SHA-256 chain verification.

Write path
----------
    audit_service.record(...)         — best-effort, never raises.
    audit_service.record_strict(...)  — raises on failure (use sparingly,
                                        only when the caller MUST know
                                        the event was persisted, e.g.
                                        platform-admin actions).

Both wrap the SQL function ``fn_append_audit_event`` which is the only
legal write path; the table has BEFORE UPDATE/DELETE triggers raising
P0002 to enforce append-only at the DB level.

Read path
---------
    list_events / get_event / verify_chain — callers MUST scope by
    merchant_id when serving merchant audiences. Admin callers pass
    merchant_id=None to get the cross-merchant view.

Naming
------
This file is the SOLE owner of the ``audit_events`` table. There is no
other "audit" service in the codebase to confuse it with.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from app.core.database import get_connection
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Row → dict mappers
# ──────────────────────────────────────────────────────────────────────
def _row_to_event(r) -> dict:
    if r is None:
        return {}
    pl = r["payload"]
    if isinstance(pl, str):
        pl = json.loads(pl)
    return {
        "id":            int(r["id"]),
        "event_uuid":    str(r["event_uuid"]),
        "merchant_id":   str(r["merchant_id"]) if r["merchant_id"] else None,
        "actor_type":    r["actor_type"],
        "actor_user_id": str(r["actor_user_id"]) if r["actor_user_id"] else None,
        "actor_label":   r["actor_label"],
        "action":        r["action"],
        "resource_type": r["resource_type"],
        "resource_id":   r["resource_id"],
        "payload":       pl or {},
        "ip_address":    str(r["ip_address"]) if r["ip_address"] else None,
        "user_agent":    r["user_agent"],
        "request_id":    r["request_id"],
        "prev_hash":     r["prev_hash"],
        "row_hash":      r["row_hash"],
        "created_at":    r["created_at"].isoformat(),
    }


def _coerce_payload(p: Any) -> str:
    """Return jsonb-friendly JSON text for any caller-supplied payload."""
    if p is None:
        return "{}"
    if isinstance(p, str):
        # Validate & normalise so the DB sees real jsonb
        return json.dumps(json.loads(p), separators=(",", ":"), sort_keys=True)
    return json.dumps(p, separators=(",", ":"), sort_keys=True, default=str)


# ──────────────────────────────────────────────────────────────────────
# Service
# ──────────────────────────────────────────────────────────────────────
_VALID_ACTOR_TYPES = ("user", "admin", "system", "cron")


class AuditService:
    # ────────────────────────────────────────────────────────────────
    # Write
    # ────────────────────────────────────────────────────────────────
    async def record_strict(
        self,
        *,
        action: str,
        actor_type: str,
        actor_user_id: Optional[str | UUID] = None,
        actor_label: Optional[str] = None,
        merchant_id: Optional[str | UUID] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        payload: Any = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        if not action or not isinstance(action, str):
            raise ValidationError("action is required")
        if actor_type not in _VALID_ACTOR_TYPES:
            raise ValidationError(
                f"actor_type must be one of {_VALID_ACTOR_TYPES}"
            )
        if user_agent and len(user_agent) > 1024:
            user_agent = user_agent[:1024]

        async with get_connection() as c:
            row = await c.fetchrow(
                """
                SELECT * FROM fn_append_audit_event(
                    $1::uuid, $2, $3::uuid, $4, $5, $6, $7,
                    $8::jsonb, $9::inet, $10, $11
                )
                """,
                str(merchant_id) if merchant_id else None,
                actor_type,
                str(actor_user_id) if actor_user_id else None,
                actor_label,
                action,
                resource_type,
                resource_id,
                _coerce_payload(payload),
                ip_address,
                user_agent,
                request_id,
            )
        return _row_to_event(row)

    async def record(self, **kwargs) -> Optional[dict]:
        """Best-effort wrapper. Logs and returns None on failure."""
        try:
            return await self.record_strict(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "audit.record_failed",
                action=kwargs.get("action"),
                error=str(exc),
            )
            return None

    # ────────────────────────────────────────────────────────────────
    # Read
    # ────────────────────────────────────────────────────────────────
    async def list_events(
        self,
        *,
        merchant_id: Optional[str | UUID] = None,
        action: Optional[str] = None,
        actor_user_id: Optional[str | UUID] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        from_ts: Optional[datetime] = None,
        to_ts: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []

        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        if action is not None:
            params.append(action)
            clauses.append(f"action = ${len(params)}")
        if actor_user_id is not None:
            params.append(str(actor_user_id))
            clauses.append(f"actor_user_id = ${len(params)}::uuid")
        if resource_type is not None:
            params.append(resource_type)
            clauses.append(f"resource_type = ${len(params)}")
        if resource_id is not None:
            params.append(resource_id)
            clauses.append(f"resource_id = ${len(params)}")
        if from_ts is not None:
            params.append(from_ts)
            clauses.append(f"created_at >= ${len(params)}")
        if to_ts is not None:
            params.append(to_ts)
            clauses.append(f"created_at <  ${len(params)}")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(min(int(limit), 500))
        params.append(int(offset))
        sql = (
            f"SELECT * FROM audit_events {where} "
            f"ORDER BY id DESC LIMIT ${len(params)-1} OFFSET ${len(params)}"
        )
        async with get_connection() as c:
            rows = await c.fetch(sql, *params)
        return [_row_to_event(r) for r in rows]

    async def get_event(
        self,
        *,
        event_uuid: str | UUID,
        merchant_id: Optional[str | UUID] = None,
    ) -> dict:
        clauses = ["event_uuid = $1::uuid"]
        params: list[Any] = [str(event_uuid)]
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        async with get_connection() as c:
            row = await c.fetchrow(
                f"SELECT * FROM audit_events WHERE {' AND '.join(clauses)}",
                *params,
            )
        if row is None:
            raise NotFoundError("audit_event", str(event_uuid))
        return _row_to_event(row)

    # ────────────────────────────────────────────────────────────────
    # Chain verification (admin)
    # ────────────────────────────────────────────────────────────────
    async def verify_chain(
        self,
        *,
        start_id: Optional[int] = None,
        end_id: Optional[int] = None,
    ) -> dict:
        if start_id is not None and end_id is not None and end_id < start_id:
            raise ValidationError("end_id must be >= start_id")
        async with get_connection() as c:
            bad = await c.fetchrow(
                "SELECT * FROM fn_verify_audit_chain($1, $2)",
                start_id, end_id,
            )
            counts = await c.fetchrow(
                """
                SELECT COUNT(*)::bigint AS checked,
                       MIN(id)         AS min_id,
                       MAX(id)         AS max_id
                  FROM audit_events
                 WHERE ($1::bigint IS NULL OR id >= $1)
                   AND ($2::bigint IS NULL OR id <= $2)
                """,
                start_id, end_id,
            )
        if bad is None:
            return {
                "ok":       True,
                "checked":  int(counts["checked"] or 0),
                "min_id":   int(counts["min_id"]) if counts["min_id"] is not None else None,
                "max_id":   int(counts["max_id"]) if counts["max_id"] is not None else None,
                "first_bad": None,
            }
        return {
            "ok":       False,
            "checked":  int(counts["checked"] or 0),
            "min_id":   int(counts["min_id"]) if counts["min_id"] is not None else None,
            "max_id":   int(counts["max_id"]) if counts["max_id"] is not None else None,
            "first_bad": {
                "id":             int(bad["bad_id"]),
                "event_uuid":     str(bad["bad_event_uuid"]),
                "expected_hash":  bad["expected_hash"],
                "stored_hash":    bad["stored_hash"],
                "expected_prev":  bad["expected_prev"],
                "stored_prev":    bad["stored_prev"],
            },
        }

    # ────────────────────────────────────────────────────────────────
    # Export
    # ────────────────────────────────────────────────────────────────
    def to_csv(self, events: list[dict]) -> dict:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "id", "event_uuid", "created_at", "merchant_id",
            "actor_type", "actor_user_id", "actor_label",
            "action", "resource_type", "resource_id",
            "ip_address", "request_id", "row_hash", "prev_hash",
            "payload",
        ])
        for e in events:
            w.writerow([
                e["id"], e["event_uuid"], e["created_at"],
                e["merchant_id"] or "",
                e["actor_type"], e["actor_user_id"] or "",
                e["actor_label"] or "",
                e["action"], e["resource_type"] or "", e["resource_id"] or "",
                e["ip_address"] or "", e["request_id"] or "",
                e["row_hash"], e["prev_hash"] or "",
                json.dumps(e["payload"], separators=(",", ":"), sort_keys=True),
            ])
        return {
            "filename":     f"audit-events-{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.csv",
            "content_type": "text/csv",
            "body":         buf.getvalue(),
        }


audit_service = AuditService()
