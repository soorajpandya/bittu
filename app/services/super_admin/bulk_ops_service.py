"""
Bulk merchant operations for Burptech super-admins.

Each operation is wrapped in a single transaction. Failures on individual
merchants are recorded per-row in the result and do not roll back others
— this is intentional: ops people need to fix the bad ones and keep
moving rather than re-run the entire batch on a partial failure.
"""
from __future__ import annotations

import csv
import io
from typing import Any, Optional

from app.core.database import get_service_connection
from app.core.logging import get_logger
from app.services.audit_service import audit_service

logger = get_logger(__name__)

_MAX_BULK = 500


async def bulk_suspend(
    *, merchant_ids: list[str], reason: str,
    actor_id: str, actor_email: Optional[str],
) -> dict[str, Any]:
    if not merchant_ids:
        raise ValueError("merchant_ids must not be empty")
    if len(merchant_ids) > _MAX_BULK:
        raise ValueError(f"max {_MAX_BULK} merchants per bulk operation")
    if not reason or len(reason.strip()) < 3:
        raise ValueError("reason must be at least 3 characters")

    succeeded: list[dict] = []
    failed: list[dict] = []
    async with get_service_connection() as conn:
        for mid in merchant_ids:
            try:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        """
                        UPDATE restaurants
                           SET suspended_at     = COALESCE(suspended_at, now()),
                               suspended_reason = $2,
                               suspended_by     = $3::uuid,
                               updated_at       = now()
                         WHERE id = $1::uuid
                     RETURNING id::text       AS restaurant_id,
                               suspended_at,
                               suspended_reason
                        """,
                        mid, reason, actor_id,
                    )
                    if not row:
                        failed.append({"merchant_id": mid, "error": "not_found"})
                        continue
                    await conn.execute(
                        """
                        INSERT INTO merchant_admin_notes
                            (merchant_id, note, author_id, author_email)
                        VALUES ($1::uuid, $2, $3::uuid, $4)
                        """,
                        mid, f"[bulk-suspended] {reason}", actor_id, actor_email,
                    )
                succeeded.append(dict(row))
            except Exception as exc:  # noqa: BLE001
                failed.append({"merchant_id": mid, "error": str(exc)[:300]})

    await _audit_bulk(
        action="super_admin.bulk_suspend", actor_id=actor_id,
        actor_email=actor_email, reason=reason,
        succeeded=succeeded, failed=failed,
    )
    return {
        "requested":   len(merchant_ids),
        "succeeded":   len(succeeded),
        "failed":      len(failed),
        "results":     succeeded,
        "failures":    failed,
    }


async def bulk_unsuspend(
    *, merchant_ids: list[str], actor_id: str, actor_email: Optional[str],
    reason: Optional[str] = None,
) -> dict[str, Any]:
    if not merchant_ids:
        raise ValueError("merchant_ids must not be empty")
    if len(merchant_ids) > _MAX_BULK:
        raise ValueError(f"max {_MAX_BULK} merchants per bulk operation")

    succeeded: list[dict] = []
    failed: list[dict] = []
    note_text = f"[bulk-unsuspended] {reason}" if reason else "[bulk-unsuspended]"
    async with get_service_connection() as conn:
        for mid in merchant_ids:
            try:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        """
                        UPDATE restaurants
                           SET suspended_at     = NULL,
                               suspended_reason = NULL,
                               suspended_by     = NULL,
                               updated_at       = now()
                         WHERE id = $1::uuid
                     RETURNING id::text AS restaurant_id
                        """,
                        mid,
                    )
                    if not row:
                        failed.append({"merchant_id": mid, "error": "not_found"})
                        continue
                    await conn.execute(
                        """
                        INSERT INTO merchant_admin_notes
                            (merchant_id, note, author_id, author_email)
                        VALUES ($1::uuid, $2, $3::uuid, $4)
                        """,
                        mid, note_text, actor_id, actor_email,
                    )
                succeeded.append(dict(row))
            except Exception as exc:  # noqa: BLE001
                failed.append({"merchant_id": mid, "error": str(exc)[:300]})

    await _audit_bulk(
        action="super_admin.bulk_unsuspend", actor_id=actor_id,
        actor_email=actor_email, reason=reason or "",
        succeeded=succeeded, failed=failed,
    )
    return {
        "requested":   len(merchant_ids),
        "succeeded":   len(succeeded),
        "failed":      len(failed),
        "results":     succeeded,
        "failures":    failed,
    }


async def bulk_add_note(
    *, merchant_ids: list[str], note: str,
    actor_id: str, actor_email: Optional[str],
) -> dict[str, Any]:
    if not merchant_ids:
        raise ValueError("merchant_ids must not be empty")
    if len(merchant_ids) > _MAX_BULK:
        raise ValueError(f"max {_MAX_BULK} merchants per bulk operation")
    if not note or len(note.strip()) < 1:
        raise ValueError("note must not be empty")

    inserted: list[str] = []
    failed: list[dict] = []
    async with get_service_connection() as conn:
        for mid in merchant_ids:
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO merchant_admin_notes
                        (merchant_id, note, author_id, author_email)
                    VALUES ($1::uuid, $2, $3::uuid, $4)
                    RETURNING id::text AS id
                    """,
                    mid, note, actor_id, actor_email,
                )
                if row:
                    inserted.append(mid)
            except Exception as exc:  # noqa: BLE001
                failed.append({"merchant_id": mid, "error": str(exc)[:300]})

    return {
        "requested":  len(merchant_ids),
        "succeeded":  len(inserted),
        "failed":     len(failed),
        "failures":   failed,
    }


async def export_kyc_csv(
    *, status: Optional[str] = None, limit: int = 5000,
) -> tuple[str, int]:
    """
    Return (csv_text, row_count). Caller should wrap in StreamingResponse
    with content-type=text/csv.
    """
    async with get_service_connection() as conn:
        if status:
            rows = await conn.fetch(
                """
                SELECT k.merchant_id::text  AS merchant_id,
                       r.name               AS merchant_name,
                       k.status::text       AS kyc_status,
                       k.business_type::text AS business_type,
                       k.legal_name,
                       k.pan, k.gstin, k.cin,
                       k.contact_email, k.contact_phone,
                       k.risk_tier,
                       k.submitted_at, k.reviewed_at, k.approved_at,
                       k.rejection_reason, k.suspension_reason,
                       k.created_at, k.updated_at
                  FROM merchant_kyc_profiles k
                  LEFT JOIN restaurants r ON r.id = k.merchant_id
                 WHERE k.status::text = $1
                 ORDER BY k.created_at DESC
                 LIMIT $2
                """,
                status, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT k.merchant_id::text  AS merchant_id,
                       r.name               AS merchant_name,
                       k.status::text       AS kyc_status,
                       k.business_type::text AS business_type,
                       k.legal_name,
                       k.pan, k.gstin, k.cin,
                       k.contact_email, k.contact_phone,
                       k.risk_tier,
                       k.submitted_at, k.reviewed_at, k.approved_at,
                       k.rejection_reason, k.suspension_reason,
                       k.created_at, k.updated_at
                  FROM merchant_kyc_profiles k
                  LEFT JOIN restaurants r ON r.id = k.merchant_id
                 ORDER BY k.created_at DESC
                 LIMIT $1
                """,
                limit,
            )

    buf = io.StringIO()
    if not rows:
        return "", 0
    fieldnames = list(rows[0].keys())
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        d = {k: ("" if v is None else str(v)) for k, v in dict(r).items()}
        writer.writerow(d)
    return buf.getvalue(), len(rows)


async def _audit_bulk(
    *, action: str, actor_id: str, actor_email: Optional[str],
    reason: str, succeeded: list, failed: list,
) -> None:
    try:
        await audit_service.record(
            action=action,
            actor_type="admin",
            actor_user_id=actor_id,
            actor_label=actor_email,
            payload={
                "reason":            reason,
                "succeeded_count":   len(succeeded),
                "failed_count":      len(failed),
                "succeeded_ids":     [s.get("restaurant_id") for s in succeeded][:50],
                "failed_ids":        [f.get("merchant_id") for f in failed][:50],
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception("audit_bulk_failed", action=action)
