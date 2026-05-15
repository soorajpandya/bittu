"""
Razorpay settlement service (Phase 6 — settlements deep-wire).

Responsibilities:
  * Mirror gateway settlement entities into `rzp_settlements`.
  * Resolve `linked_account_id` → Bittu `merchant_id` via `rzp_route_accounts`.
  * Pull and persist the daily recon report (`/v1/settlements/recon/combined`)
    into `rzp_settlement_payments` so each Bittu payment can be traced back
    to the settlement that swept it.
  * Provide local read APIs that the merchant-facing router consumes.

Idempotency contract:
  * `rzp_settlements` is keyed on `settlement_id` (UNIQUE) — UPSERT.
  * `rzp_settlement_payments` is keyed on
    `(settlement_id, razorpay_payment_id, type)` (UNIQUE) — INSERT … ON CONFLICT
    DO NOTHING. Table is append-only (trigger `trg_rzp_setl_pay_no_mutate`),
    so we never UPDATE.

Cross-merchant: every write uses `get_service_connection()` (rzp_* tables
have RLS off; resolution is webhook-driven).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from app.core.database import get_connection, get_service_connection
from app.core.logging import get_logger

logger = get_logger(__name__)

_PLATFORM_MERCHANT_UUID = "00000000-0000-0000-0000-000000000000"


def _row_to_settlement(r) -> Optional[dict]:
    if r is None:
        return None
    return {
        "id":                str(r["id"]),
        "settlement_id":     r["settlement_id"],
        "merchant_id":       str(r["merchant_id"]),
        "linked_account_id": r["linked_account_id"],
        "amount_paise":      int(r["amount_paise"]),
        "fees_paise":        int(r["fees_paise"]),
        "tax_paise":         int(r["tax_paise"]),
        "utr":               r["utr"],
        "status":            r["status"],
        "settled_at":        r["settled_at"].isoformat() if r["settled_at"] else None,
        "created_for_date":  r["created_for_date"].isoformat() if r["created_for_date"] else None,
        "created_at":        r["created_at"].isoformat(),
        "updated_at":        r["updated_at"].isoformat(),
    }


def _row_to_settlement_payment(r) -> dict:
    return {
        "id":                  int(r["id"]),
        "settlement_id":       r["settlement_id"],
        "razorpay_payment_id": r["razorpay_payment_id"],
        "merchant_id":         str(r["merchant_id"]),
        "type":                r["type"],
        "amount_paise":        int(r["amount_paise"]),
        "fee_paise":           int(r["fee_paise"]),
        "tax_paise":           int(r["tax_paise"]),
        "debit_paise":         int(r["debit_paise"]),
        "credit_paise":        int(r["credit_paise"]),
        "created_at":          r["created_at"].isoformat(),
    }


class RzpSettlementService:
    """Razorpay settlement reconciliation service."""

    # ── Resolution helpers ─────────────────────────────────────────────────

    async def _resolve_merchant_from_linked_account(
        self, linked_account_id: Optional[str]
    ) -> Optional[str]:
        """Look up the Bittu merchant that owns a Razorpay linked account."""
        if not linked_account_id:
            return None
        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                "SELECT merchant_id::text AS merchant_id "
                "FROM rzp_route_accounts WHERE linked_account_id = $1 LIMIT 1",
                linked_account_id,
            )
        return row["merchant_id"] if row else None

    async def _resolve_merchant_from_payment(
        self, razorpay_payment_id: str
    ) -> Optional[str]:
        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT p.merchant_id::text AS merchant_id
                FROM rzp_payments_index i
                JOIN rzp_payments p ON p.id = i.payment_uuid
                WHERE i.razorpay_payment_id = $1
                LIMIT 1
                """,
                razorpay_payment_id,
            )
        return row["merchant_id"] if row else None

    # ── Gateway-state mirror ───────────────────────────────────────────────

    async def upsert_from_razorpay(
        self,
        *,
        rzp_entity: dict,
        merchant_id_override: Optional[str] = None,
        status_override: Optional[str] = None,
    ) -> Optional[dict]:
        """
        UPSERT a settlement entity from Razorpay.

        Resolution: ``merchant_id_override`` wins; otherwise look up via
        ``linked_account_id`` in ``rzp_route_accounts``; otherwise the
        platform placeholder UUID (Phase 13 recon will backfill).
        """
        settlement_id = rzp_entity.get("id")
        if not settlement_id:
            return None

        linked_account_id = rzp_entity.get("linked_account_id")
        merchant_id = (
            merchant_id_override
            or await self._resolve_merchant_from_linked_account(linked_account_id)
            or _PLATFORM_MERCHANT_UUID
        )

        status = (status_override or rzp_entity.get("status") or "pending").lower()
        if status not in ("pending", "processing", "processed", "failed", "reversed"):
            status = "pending"

        settled_at_epoch = rzp_entity.get("settled_at") or rzp_entity.get("created_at")
        raw = json.dumps(rzp_entity)

        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO rzp_settlements (
                    settlement_id, merchant_id, linked_account_id,
                    amount_paise, fees_paise, tax_paise,
                    utr, status, settled_at, raw_payload
                ) VALUES (
                    $1, $2::uuid, $3,
                    $4, $5, $6,
                    $7, $8::rzp_settlement_state,
                    CASE WHEN $9::bigint IS NULL THEN NULL
                         ELSE to_timestamp($9::bigint) END,
                    $10::jsonb
                )
                ON CONFLICT (settlement_id) DO UPDATE SET
                    merchant_id       = CASE
                        WHEN rzp_settlements.merchant_id = $11::uuid
                        THEN EXCLUDED.merchant_id
                        ELSE rzp_settlements.merchant_id
                    END,
                    linked_account_id = COALESCE(EXCLUDED.linked_account_id,
                                                 rzp_settlements.linked_account_id),
                    amount_paise = EXCLUDED.amount_paise,
                    fees_paise   = EXCLUDED.fees_paise,
                    tax_paise    = EXCLUDED.tax_paise,
                    utr          = COALESCE(EXCLUDED.utr, rzp_settlements.utr),
                    status       = EXCLUDED.status,
                    settled_at   = COALESCE(EXCLUDED.settled_at,
                                            rzp_settlements.settled_at),
                    raw_payload  = EXCLUDED.raw_payload,
                    updated_at   = NOW()
                RETURNING *
                """,
                settlement_id, merchant_id, linked_account_id,
                int(rzp_entity.get("amount") or 0),
                int(rzp_entity.get("fees") or 0),
                int(rzp_entity.get("tax") or 0),
                rzp_entity.get("utr"),
                status,
                settled_at_epoch, raw,
                _PLATFORM_MERCHANT_UUID,
            )
        return _row_to_settlement(row)

    async def sync_settlement(
        self, settlement_id: str, *, merchant_id: str
    ) -> Optional[dict]:
        """Refetch a single settlement from the gateway and re-upsert."""
        from app.services.razorpay import settlements as rzp_settlements_api
        rzp_entity = await rzp_settlements_api.fetch_settlement(
            settlement_id, merchant_id=merchant_id,
        )
        return await self.upsert_from_razorpay(
            rzp_entity=rzp_entity,
            merchant_id_override=merchant_id,
        )

    # ── Recon report ingest ────────────────────────────────────────────────

    async def fetch_recon_and_persist(
        self,
        *,
        year: int,
        month: int,
        day: Optional[int] = None,
        merchant_id: Optional[str] = None,
        page_size: int = 1000,
    ) -> dict:
        """
        Pull `/v1/settlements/recon/combined` and persist every row into
        `rzp_settlement_payments`. Returns counters.

        ``merchant_id`` is forwarded to the client so the
        ``X-Razorpay-Account`` header is set when Route is in play.
        """
        from app.services.razorpay import settlements as rzp_settlements_api

        skip = 0
        seen = 0
        inserted = 0
        while True:
            resp = await rzp_settlements_api.list_settlement_recon(
                year=year, month=month, day=day,
                count=page_size, skip=skip,
                merchant_id=merchant_id,
            )
            items = (resp or {}).get("items") or []
            if not items:
                break

            for item in items:
                seen += 1
                try:
                    if await self._persist_recon_row(item, merchant_id_hint=merchant_id):
                        inserted += 1
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "rzp_recon_row_persist_failed",
                        settlement_id=item.get("settlement_id"),
                        razorpay_payment_id=item.get("entity_id"),
                    )

            if len(items) < page_size:
                break
            skip += page_size

        return {
            "year": year, "month": month, "day": day,
            "seen": seen, "inserted": inserted,
        }

    async def _persist_recon_row(
        self, row: dict, *, merchant_id_hint: Optional[str]
    ) -> bool:
        """
        INSERT a recon row. Returns True if a new row was inserted, False on
        ON CONFLICT DO NOTHING (already present).
        """
        settlement_id = row.get("settlement_id")
        razorpay_payment_id = row.get("entity_id") or row.get("payment_id")
        type_ = row.get("type") or "payment"
        if not settlement_id or not razorpay_payment_id:
            return False

        merchant_id = (
            merchant_id_hint
            or await self._resolve_merchant_from_payment(razorpay_payment_id)
            or _PLATFORM_MERCHANT_UUID
        )

        async with get_service_connection() as conn:
            res = await conn.execute(
                """
                INSERT INTO rzp_settlement_payments (
                    settlement_id, razorpay_payment_id, merchant_id,
                    type, amount_paise, fee_paise, tax_paise,
                    debit_paise, credit_paise, raw_row
                ) VALUES (
                    $1, $2, $3::uuid,
                    $4, $5, $6, $7,
                    $8, $9, $10::jsonb
                )
                ON CONFLICT (settlement_id, razorpay_payment_id, type) DO NOTHING
                """,
                settlement_id, razorpay_payment_id, merchant_id,
                type_,
                int(row.get("amount") or 0),
                int(row.get("fee") or 0),
                int(row.get("tax") or 0),
                int(row.get("debit") or 0),
                int(row.get("credit") or 0),
                json.dumps(row),
            )
        # asyncpg returns "INSERT 0 1" on success, "INSERT 0 0" on conflict.
        return res.endswith(" 1")

    # ── Local read APIs (consumed by REST router) ──────────────────────────

    async def list_settlements(
        self,
        *,
        merchant_id: str,
        status: Optional[str] = None,
        from_ts=None,
        to_ts=None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        clauses = ["merchant_id = $1::uuid"]
        params: list[Any] = [str(merchant_id)]
        if status is not None:
            params.append(status)
            clauses.append(f"status = ${len(params)}::rzp_settlement_state")
        if from_ts is not None:
            params.append(from_ts); clauses.append(f"created_at >= ${len(params)}")
        if to_ts is not None:
            params.append(to_ts); clauses.append(f"created_at <  ${len(params)}")

        params.append(min(int(limit), 200))
        params.append(int(offset))
        sql = (
            f"SELECT * FROM rzp_settlements WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at DESC "
            f"LIMIT ${len(params)-1} OFFSET ${len(params)}"
        )
        async with get_connection() as c:
            rows = await c.fetch(sql, *params)
        return [_row_to_settlement(r) for r in rows]

    async def get_settlement(
        self, settlement_id: str, *, merchant_id: str
    ) -> Optional[dict]:
        async with get_connection() as c:
            row = await c.fetchrow(
                "SELECT * FROM rzp_settlements "
                "WHERE settlement_id = $1 AND merchant_id = $2::uuid LIMIT 1",
                settlement_id, str(merchant_id),
            )
        return _row_to_settlement(row)

    async def list_settlement_payments(
        self,
        settlement_id: str,
        *,
        merchant_id: str,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict]:
        async with get_connection() as c:
            rows = await c.fetch(
                """
                SELECT * FROM rzp_settlement_payments
                WHERE settlement_id = $1 AND merchant_id = $2::uuid
                ORDER BY id ASC
                LIMIT $3 OFFSET $4
                """,
                settlement_id, str(merchant_id),
                min(int(limit), 2000), int(offset),
            )
        return [_row_to_settlement_payment(r) for r in rows]


rzp_settlement_service = RzpSettlementService()
