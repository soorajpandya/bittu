"""
Settlements (admin) — cross-merchant settlement list + immutable timeline.

Prefix:   /admin/settlements
Audience: platform admins.

Reads:
  • bittu_settlements           — header rows
  • bittu_settlement_timeline   — append-only event log per settlement
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import require_platform_admin
from app.core.database import get_connection

router = APIRouter(prefix="/admin/settlements", tags=["Settlements (Admin)"])


@router.get("")
async def list_settlements(
    merchant_id: Optional[str]      = Query(None),
    branch_id:   Optional[str]      = Query(None),
    status:      Optional[str]      = Query(None,
        description="pending|processing|sent_to_bank|settled|failed|reversed"),
    settlement_reference: Optional[str] = Query(None),
    bank_reference_number: Optional[str] = Query(None),
    from_ts:     Optional[datetime] = Query(None),
    to_ts:       Optional[datetime] = Query(None),
    limit:       int                = Query(100, ge=1, le=500),
    offset:      int                = Query(0, ge=0),
    _ = Depends(require_platform_admin()),
):
    where: list[str] = []
    args:  list = []
    def add(clause: str, value):
        args.append(value)
        where.append(clause.replace("?", f"${len(args)}"))
    if merchant_id: add("s.restaurant_id = ?::uuid", merchant_id)
    if branch_id:   add("s.branch_id = ?::uuid", branch_id)
    if status:      add("s.settlement_status = ?", status)
    if settlement_reference:  add("s.settlement_reference = ?", settlement_reference)
    if bank_reference_number: add("s.bank_reference_number = ?", bank_reference_number)
    if from_ts:     add("s.created_at >= ?", from_ts)
    if to_ts:       add("s.created_at <  ?", to_ts)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT s.id::text                  AS settlement_id,
               s.settlement_reference,
               s.restaurant_id::text       AS restaurant_id,
               r.name                      AS restaurant_name,
               u.email                     AS owner_email,
               s.branch_id::text           AS branch_id,
               s.gross_amount,
               s.bittu_fee_amount,
               s.gst_amount,
               s.net_settlement_amount,
               s.fee_rate,
               s.gst_rate,
               s.settlement_status,
               s.settlement_cycle,
               s.expected_settlement_at,
               s.settled_at,
               s.bank_reference_number,
               s.retry_count,
               s.failure_reason,
               s.last_attempt_at,
               s.period_start,
               s.period_end,
               s.created_at,
               s.updated_at
          FROM bittu_settlements s
          LEFT JOIN restaurants r ON r.id = s.restaurant_id
          LEFT JOIN auth.users  u ON u.id::text = r.owner_id::text
          {where_sql}
         ORDER BY s.created_at DESC
         LIMIT ${len(args)+1} OFFSET ${len(args)+2}
    """
    count_sql = f"SELECT count(*) FROM bittu_settlements s {where_sql}"

    async with get_connection() as conn:
        rows  = await conn.fetch(sql, *args, limit, offset)
        total = await conn.fetchval(count_sql, *args)
    return {
        "items":  [dict(r) for r in rows],
        "limit":  limit,
        "offset": offset,
        "total":  total,
    }


@router.get("/{settlement_id}")
async def get_settlement(
    settlement_id: str,
    _ = Depends(require_platform_admin()),
):
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT s.*,
                   r.name  AS restaurant_name,
                   u.email AS owner_email
              FROM bittu_settlements s
              LEFT JOIN restaurants r ON r.id = s.restaurant_id
              LEFT JOIN auth.users  u ON u.id::text = r.owner_id::text
             WHERE s.id = $1::uuid
            """,
            settlement_id,
        )
    if not row:
        raise HTTPException(404, "Settlement not found")
    out = dict(row)
    # Normalize UUID columns to text for JSON.
    for k in ("id", "restaurant_id", "branch_id", "journal_entry_id"):
        if out.get(k) is not None:
            out[k] = str(out[k])
    return out


@router.get("/{settlement_id}/timeline")
async def get_settlement_timeline(
    settlement_id: str,
    limit: int = Query(200, ge=1, le=1000),
    _ = Depends(require_platform_admin()),
):
    """
    Immutable event log for a settlement (created → processing → sent_to_bank
    → settled / failed / reversed). Ordered oldest-first so the cockpit
    can render a top-down vertical timeline without re-sorting.
    """
    async with get_connection() as conn:
        # Confirm settlement exists so 404 is distinguishable from "no events".
        exists = await conn.fetchval(
            "SELECT 1 FROM bittu_settlements WHERE id = $1::uuid", settlement_id
        )
        if not exists:
            raise HTTPException(404, "Settlement not found")
        rows = await conn.fetch(
            """
            SELECT id::text,
                   event_type,
                   title,
                   description,
                   from_status,
                   to_status,
                   actor_id,
                   actor_type,
                   metadata,
                   occurred_at
              FROM bittu_settlement_timeline
             WHERE settlement_id = $1::uuid
             ORDER BY occurred_at ASC
             LIMIT $2
            """,
            settlement_id, limit,
        )
    return {
        "settlement_id": settlement_id,
        "items":         [dict(r) for r in rows],
        "count":         len(rows),
    }
