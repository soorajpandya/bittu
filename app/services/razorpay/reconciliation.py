"""
Razorpay reconciliation engine (Phase 9 — MVP).

3-way matcher: Payments ↔ Transfers ↔ Settlements.

For each run we open a `rzp_reconciliation_runs` row, scan a time window
across the canonical Razorpay mirror tables (`rzp_payments`,
`rzp_route_transfers`, `rzp_settlements`, `rzp_refunds`) and insert any
mismatch as a row in `rzp_reconciliation_discrepancies`.

The engine is intentionally read-only on business tables — it never
mutates payments / transfers / settlements. Resolution is a human action
via the super-admin endpoints.

Detection categories (see migration 069 for the enum-like comment):
  - payment_without_transfer            captured payment with linked-account
                                        merchant but no rzp_route_transfers row
  - transfer_without_payment            transfer with razorpay_payment_id
                                        absent from rzp_payments
  - amount_mismatch_payment_transfer    transfer.amount_paise vs payment.amount_paise
                                        diverges from the configured fee split
                                        by more than `MAX_FEE_SHARE_PCT`
  - transfer_without_settlement         transfer.status='processed' and older
                                        than T+settlement_grace_days but no
                                        recipient_settlement_id
  - refund_without_reversal             rzp_refunds.status='processed' but
                                        the source transfer is not reversed
                                        within the window
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.core.database import get_service_connection
from app.core.logging import get_logger

logger = get_logger(__name__)


# Default tolerance — Bittu fee is configurable per-merchant via
# `fee_service.compute_fee`, so we cannot hard-code the expected split here.
# Instead we tolerate up to `MAX_FEE_SHARE_PCT` of the payment as legitimate
# Bittu fee + GST. Anything beyond that is flagged.
MAX_FEE_SHARE_PCT = 20  # 20% — generous upper bound; real share is ~6-8%.
SETTLEMENT_GRACE_DAYS = 4


class RzpReconciliationService:
    async def run_daily_reconciliation(
        self,
        *,
        window_from: Optional[datetime] = None,
        window_to: Optional[datetime] = None,
        triggered_by: str = "scheduler",
        actor_user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Run a single reconciliation pass over the given window.

        Default window: yesterday 00:00 UTC → today 00:00 UTC.
        """
        now = datetime.now(timezone.utc)
        if window_to is None:
            window_to = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if window_from is None:
            window_from = window_to - timedelta(days=1)

        run_id = await self._open_run(
            window_from=window_from,
            window_to=window_to,
            triggered_by=triggered_by,
            actor_user_id=actor_user_id,
        )
        logger.info(
            "rzp_recon_run_started",
            run_id=run_id,
            window_from=window_from.isoformat(),
            window_to=window_to.isoformat(),
        )

        try:
            counts = await self._execute(
                run_id=run_id,
                window_from=window_from,
                window_to=window_to,
            )
            await self._close_run(run_id, status="completed", counts=counts)
            logger.info("rzp_recon_run_completed", run_id=run_id, **counts)
            return {"run_id": run_id, **counts}
        except Exception as exc:  # noqa: BLE001
            logger.exception("rzp_recon_run_failed", run_id=run_id)
            await self._close_run(
                run_id, status="failed", counts={}, error=str(exc),
            )
            return {"run_id": run_id, "error": str(exc)}

    # ── Run lifecycle ───────────────────────────────────────────────────

    async def _open_run(
        self,
        *,
        window_from: datetime,
        window_to: datetime,
        triggered_by: str,
        actor_user_id: Optional[str],
    ) -> str:
        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO rzp_reconciliation_runs
                    (window_from, window_to, triggered_by, actor_user_id, status)
                VALUES ($1, $2, $3, $4::uuid, 'running')
                RETURNING id::text AS id
                """,
                window_from, window_to, triggered_by, actor_user_id,
            )
        return row["id"]

    async def _close_run(
        self,
        run_id: str,
        *,
        status: str,
        counts: dict[str, int],
        error: Optional[str] = None,
    ) -> None:
        async with get_service_connection() as conn:
            await conn.execute(
                """
                UPDATE rzp_reconciliation_runs
                   SET status              = $2,
                       payments_scanned    = COALESCE($3, payments_scanned),
                       transfers_scanned   = COALESCE($4, transfers_scanned),
                       settlements_scanned = COALESCE($5, settlements_scanned),
                       discrepancies_found = COALESCE($6, discrepancies_found),
                       error_message       = $7,
                       run_completed_at    = NOW()
                 WHERE id = $1::uuid
                """,
                run_id, status,
                counts.get("payments_scanned"),
                counts.get("transfers_scanned"),
                counts.get("settlements_scanned"),
                counts.get("discrepancies_found"),
                error,
            )

    # ── Core detection ──────────────────────────────────────────────────

    async def _execute(
        self,
        *,
        run_id: str,
        window_from: datetime,
        window_to: datetime,
    ) -> dict[str, int]:
        async with get_service_connection() as conn:
            payments_scanned = await conn.fetchval(
                "SELECT COUNT(*) FROM rzp_payments "
                "WHERE created_at >= $1 AND created_at < $2 AND status = 'captured'",
                window_from, window_to,
            )
            transfers_scanned = await conn.fetchval(
                "SELECT COUNT(*) FROM rzp_route_transfers "
                "WHERE created_at >= $1 AND created_at < $2",
                window_from, window_to,
            )
            settlements_scanned = await conn.fetchval(
                "SELECT COUNT(*) FROM rzp_settlements "
                "WHERE created_at >= $1 AND created_at < $2",
                window_from, window_to,
            )

        discrepancies = 0
        discrepancies += await self._detect_payment_without_transfer(
            run_id=run_id, window_from=window_from, window_to=window_to,
        )
        discrepancies += await self._detect_transfer_without_payment(
            run_id=run_id, window_from=window_from, window_to=window_to,
        )
        discrepancies += await self._detect_amount_mismatch(
            run_id=run_id, window_from=window_from, window_to=window_to,
        )
        discrepancies += await self._detect_transfer_without_settlement(
            run_id=run_id, window_from=window_from, window_to=window_to,
        )
        discrepancies += await self._detect_refund_without_reversal(
            run_id=run_id, window_from=window_from, window_to=window_to,
        )

        return {
            "payments_scanned":    int(payments_scanned or 0),
            "transfers_scanned":   int(transfers_scanned or 0),
            "settlements_scanned": int(settlements_scanned or 0),
            "discrepancies_found": int(discrepancies),
        }

    async def _detect_payment_without_transfer(
        self, *, run_id: str, window_from: datetime, window_to: datetime,
    ) -> int:
        # Only flag merchants who actually have an activated Route account —
        # legacy / non-Route merchants don't get a transfer by design.
        async with get_service_connection() as conn:
            rows = await conn.fetch(
                """
                WITH eligible AS (
                  SELECT p.razorpay_payment_id, p.merchant_id, p.amount_paise
                    FROM rzp_payments p
                    JOIN rzp_route_accounts a
                      ON a.merchant_id = p.merchant_id
                     AND a.status = 'activated'
                   WHERE p.created_at >= $2
                     AND p.created_at <  $3
                     AND p.status = 'captured'
                ), missing AS (
                  SELECT e.*
                    FROM eligible e
                    LEFT JOIN rzp_route_transfers t
                           ON t.razorpay_payment_id = e.razorpay_payment_id
                   WHERE t.transfer_id IS NULL
                )
                INSERT INTO rzp_reconciliation_discrepancies
                    (run_id, merchant_id, restaurant_id, discrepancy_type,
                     severity, razorpay_payment_id, expected_amount_paise,
                     actual_amount_paise, variance_paise, details)
                SELECT $1::uuid, m.merchant_id, m.merchant_id,
                       'payment_without_transfer',
                       'high',
                       m.razorpay_payment_id,
                       m.amount_paise,
                       0,
                       m.amount_paise,
                       jsonb_build_object('note', 'no transfer row found for captured payment')
                  FROM missing m
                ON CONFLICT DO NOTHING
                RETURNING 1
                """,
                run_id, window_from, window_to,
            )
        return len(rows)

    async def _detect_transfer_without_payment(
        self, *, run_id: str, window_from: datetime, window_to: datetime,
    ) -> int:
        async with get_service_connection() as conn:
            rows = await conn.fetch(
                """
                WITH orphans AS (
                  SELECT t.transfer_id, t.razorpay_payment_id, t.merchant_id,
                         t.amount_paise
                    FROM rzp_route_transfers t
                    LEFT JOIN rzp_payments p
                           ON p.razorpay_payment_id = t.razorpay_payment_id
                   WHERE t.created_at >= $2
                     AND t.created_at <  $3
                     AND t.razorpay_payment_id IS NOT NULL
                     AND t.razorpay_payment_id <> ''
                     AND p.razorpay_payment_id IS NULL
                )
                INSERT INTO rzp_reconciliation_discrepancies
                    (run_id, merchant_id, restaurant_id, discrepancy_type,
                     severity, razorpay_payment_id, transfer_id,
                     actual_amount_paise, details)
                SELECT $1::uuid, o.merchant_id, o.merchant_id,
                       'transfer_without_payment',
                       'critical',
                       o.razorpay_payment_id,
                       o.transfer_id,
                       o.amount_paise,
                       jsonb_build_object('note', 'transfer references unknown payment')
                  FROM orphans o
                ON CONFLICT DO NOTHING
                RETURNING 1
                """,
                run_id, window_from, window_to,
            )
        return len(rows)

    async def _detect_amount_mismatch(
        self, *, run_id: str, window_from: datetime, window_to: datetime,
    ) -> int:
        async with get_service_connection() as conn:
            rows = await conn.fetch(
                """
                WITH joined AS (
                  SELECT t.transfer_id, t.razorpay_payment_id, t.merchant_id,
                         t.amount_paise   AS transfer_paise,
                         p.amount_paise   AS payment_paise
                    FROM rzp_route_transfers t
                    JOIN rzp_payments p
                      ON p.razorpay_payment_id = t.razorpay_payment_id
                   WHERE t.created_at >= $2
                     AND t.created_at <  $3
                     AND t.status IN ('created', 'processed')
                     AND p.status = 'captured'
                ), bad AS (
                  SELECT j.*,
                         (j.payment_paise - j.transfer_paise) AS fee_share_paise
                    FROM joined j
                   WHERE j.transfer_paise > j.payment_paise
                      OR (j.payment_paise - j.transfer_paise) * 100
                         > j.payment_paise * $4
                )
                INSERT INTO rzp_reconciliation_discrepancies
                    (run_id, merchant_id, restaurant_id, discrepancy_type,
                     severity, razorpay_payment_id, transfer_id,
                     expected_amount_paise, actual_amount_paise, variance_paise,
                     details)
                SELECT $1::uuid, b.merchant_id, b.merchant_id,
                       'amount_mismatch_payment_transfer',
                       CASE WHEN b.transfer_paise > b.payment_paise
                            THEN 'critical' ELSE 'medium' END,
                       b.razorpay_payment_id,
                       b.transfer_id,
                       b.payment_paise,
                       b.transfer_paise,
                       b.fee_share_paise,
                       jsonb_build_object(
                          'note', 'transfer amount diverges from payment amount beyond fee bound',
                          'max_fee_share_pct', $4
                       )
                  FROM bad b
                ON CONFLICT DO NOTHING
                RETURNING 1
                """,
                run_id, window_from, window_to, MAX_FEE_SHARE_PCT,
            )
        return len(rows)

    async def _detect_transfer_without_settlement(
        self, *, run_id: str, window_from: datetime, window_to: datetime,
    ) -> int:
        cutoff = window_to - timedelta(days=SETTLEMENT_GRACE_DAYS)
        async with get_service_connection() as conn:
            rows = await conn.fetch(
                """
                WITH stale AS (
                  SELECT t.transfer_id, t.razorpay_payment_id, t.merchant_id,
                         t.amount_paise, t.processed_at
                    FROM rzp_route_transfers t
                   WHERE t.status = 'processed'
                     AND t.processed_at IS NOT NULL
                     AND t.processed_at <  $2
                     AND t.recipient_settlement_id IS NULL
                )
                INSERT INTO rzp_reconciliation_discrepancies
                    (run_id, merchant_id, restaurant_id, discrepancy_type,
                     severity, razorpay_payment_id, transfer_id,
                     actual_amount_paise, details)
                SELECT $1::uuid, s.merchant_id, s.merchant_id,
                       'transfer_without_settlement',
                       'high',
                       s.razorpay_payment_id,
                       s.transfer_id,
                       s.amount_paise,
                       jsonb_build_object(
                         'processed_at', s.processed_at,
                         'grace_days', $3::int
                       )
                  FROM stale s
                ON CONFLICT DO NOTHING
                RETURNING 1
                """,
                run_id, cutoff, SETTLEMENT_GRACE_DAYS,
            )
        return len(rows)

    async def _detect_refund_without_reversal(
        self, *, run_id: str, window_from: datetime, window_to: datetime,
    ) -> int:
        async with get_service_connection() as conn:
            rows = await conn.fetch(
                """
                WITH refund_only AS (
                  SELECT r.refund_id, r.razorpay_payment_id, r.merchant_id,
                         r.amount_paise
                    FROM rzp_refunds r
                   WHERE r.status = 'processed'
                     AND r.processed_at >= $2
                     AND r.processed_at <  $3
                     AND EXISTS (
                       SELECT 1 FROM rzp_route_transfers t
                        WHERE t.razorpay_payment_id = r.razorpay_payment_id
                          AND t.merchant_id <> '00000000-0000-0000-0000-000000000000'::uuid
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM rzp_route_transfers tr
                        WHERE tr.razorpay_payment_id = r.razorpay_payment_id
                          AND (tr.status = 'reversed' OR tr.refund_id = r.refund_id)
                     )
                )
                INSERT INTO rzp_reconciliation_discrepancies
                    (run_id, merchant_id, restaurant_id, discrepancy_type,
                     severity, razorpay_payment_id, refund_id,
                     actual_amount_paise, details)
                SELECT $1::uuid, ro.merchant_id, ro.merchant_id,
                       'refund_without_reversal',
                       'high',
                       ro.razorpay_payment_id,
                       ro.refund_id,
                       ro.amount_paise,
                       jsonb_build_object('note', 'refund processed but no transfer reversal recorded')
                  FROM refund_only ro
                ON CONFLICT DO NOTHING
                RETURNING 1
                """,
                run_id, window_from, window_to,
            )
        return len(rows)

    # ── Admin read helpers ──────────────────────────────────────────────

    async def list_runs(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        async with get_service_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT id::text AS id, run_started_at, run_completed_at,
                       window_from, window_to, status,
                       payments_scanned, transfers_scanned, settlements_scanned,
                       discrepancies_found, triggered_by, error_message
                  FROM rzp_reconciliation_runs
                 ORDER BY run_started_at DESC
                 LIMIT $1 OFFSET $2
                """,
                limit, offset,
            )
        return [dict(r) for r in rows]

    async def get_run(self, run_id: str) -> Optional[dict]:
        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT id::text AS id, run_started_at, run_completed_at,
                       window_from, window_to, status,
                       payments_scanned, transfers_scanned, settlements_scanned,
                       discrepancies_found, triggered_by, error_message,
                       metadata
                  FROM rzp_reconciliation_runs
                 WHERE id = $1::uuid
                """,
                run_id,
            )
        return dict(row) if row else None

    async def list_discrepancies(
        self,
        *,
        run_id: Optional[str] = None,
        discrepancy_type: Optional[str] = None,
        status: Optional[str] = None,
        merchant_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id:
            params.append(run_id)
            clauses.append(f"run_id = ${len(params)}::uuid")
        if discrepancy_type:
            params.append(discrepancy_type)
            clauses.append(f"discrepancy_type = ${len(params)}")
        if status:
            params.append(status)
            clauses.append(f"status = ${len(params)}")
        if merchant_id:
            params.append(merchant_id)
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])
        sql = f"""
            SELECT id::text AS id, run_id::text AS run_id,
                   merchant_id::text AS merchant_id,
                   discrepancy_type, severity,
                   razorpay_payment_id, transfer_id, settlement_id, refund_id,
                   expected_amount_paise, actual_amount_paise, variance_paise,
                   status, details, created_at, resolved_at, resolution_note
              FROM rzp_reconciliation_discrepancies
              {where}
             ORDER BY created_at DESC
             LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """
        async with get_service_connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    async def update_discrepancy_status(
        self,
        *,
        discrepancy_id: str,
        new_status: str,
        resolved_by_user_id: Optional[str] = None,
        resolution_note: Optional[str] = None,
    ) -> Optional[dict]:
        if new_status not in {"open", "investigating", "resolved", "ignored"}:
            raise ValueError(f"invalid status: {new_status!r}")
        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                """
                UPDATE rzp_reconciliation_discrepancies
                   SET status              = $2,
                       resolved_by_user_id = $3::uuid,
                       resolution_note     = COALESCE($4, resolution_note),
                       resolved_at         = CASE
                           WHEN $2 IN ('resolved', 'ignored') THEN NOW()
                           ELSE NULL END,
                       updated_at          = NOW()
                 WHERE id = $1::uuid
                 RETURNING id::text AS id, status, resolved_at, resolution_note
                """,
                discrepancy_id, new_status, resolved_by_user_id, resolution_note,
            )
        return dict(row) if row else None


rzp_reconciliation_service = RzpReconciliationService()
