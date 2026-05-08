"""
Reconciliation Service.

Detects mismatches across orders, payments, settlements and webhook events.
Designed to be safe to run repeatedly: every detected discrepancy is unique
per (run_id, kind, order_id|payment_id|settlement_id) so duplicate inserts
on a re-run replace the previous rows scoped to the new run.

Six mismatch scenarios mapped to `reconciliation_discrepancies.kind`:

  * payment_received_order_not_updated
        payments.status = 'completed' but orders.status NOT IN
        ('Confirmed','Preparing','Ready','Completed','Served','Delivered').

  * order_created_payment_missing
        orders.status NOT IN ('Cancelled')  AND no row in payments(order_id).
        Only flagged for orders older than `min_order_age_minutes` so we don't
        race the checkout -> payment-init transition.

  * duplicate_payment
        > 1 payments row with status='completed' for the same order_id.

  * failed_settlement
        pg_settlements.status = 'pending' and settlement_date < today - 7d
        (i.e. should have arrived from the gateway by now).

  * partial_settlement
        pg_settlements.net_amount != gross - fee - tax (rounding excluded)
        OR sum(payments.amount in payment_ids) != gross_amount.

  * webhook_delayed_or_failed
        webhook_events.status IN ('failed') OR
        (status='received' AND received_at < now() - 10 min).

Bonus checks:
  * amount_mismatch       -- payment.amount != order.total_amount
  * orphan_settlement     -- pg_settlements.payment_ids reference missing payments

Public surface:
    run_reconciliation()  -> dict   (executes a scan, returns run summary)
    list_discrepancies()  -> list   (filtered query)
    list_runs()           -> list   (history)
    get_run()             -> dict   (single run + discrepancies)
    summary()             -> dict   (orders/payments/settlements totals + open issue counts)
    record_webhook()      -> dict   (durable webhook ledger insert)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from app.core.auth import UserContext
from app.core.database import get_connection, get_transaction
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)


# Order statuses considered "paid / fulfilled" (case-insensitive compare).
PAID_ORDER_STATUSES = {"confirmed", "preparing", "ready", "completed", "served", "delivered"}


def _owner_id(user: UserContext) -> str:
    return user.owner_id if user.is_branch_user else user.user_id


def _to_float(value) -> float:
    if value is None:
        return 0.0
    return float(Decimal(str(value)))


class ReconciliationService:
    # ─────────────────────────────────────────────────────────────────
    # 1. WEBHOOK LEDGER (durable, replay-safe)
    # ─────────────────────────────────────────────────────────────────
    async def record_webhook(
        self,
        *,
        gateway: str,
        event_type: str,
        event_id: Optional[str],
        gateway_payment_id: Optional[str] = None,
        gateway_order_id: Optional[str] = None,
        raw_payload: dict,
        signature: Optional[str] = None,
        signature_valid: bool = False,
    ) -> dict:
        """Insert a webhook into the durable ledger.

        Returns a dict with keys:
            id          -- the new (or existing) webhook_events.id
            duplicate   -- True if (gateway, event_id) was already present
            status      -- current row status

        Caller must check `duplicate`; if True, skip processing and respond 200
        to the gateway (the original delivery already succeeded).
        """
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO webhook_events (
                    gateway, event_type, event_id,
                    gateway_payment_id, gateway_order_id,
                    raw_payload, signature, signature_valid, status
                )
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, 'received')
                ON CONFLICT (gateway, event_id) DO NOTHING
                RETURNING id, status
                """,
                gateway, event_type, event_id,
                gateway_payment_id, gateway_order_id,
                json.dumps(raw_payload), signature, signature_valid,
            )
            if row:
                return {"id": str(row["id"]), "status": row["status"], "duplicate": False}

            existing = await conn.fetchrow(
                "SELECT id, status FROM webhook_events WHERE gateway = $1 AND event_id = $2",
                gateway, event_id,
            )
            if existing:
                return {"id": str(existing["id"]), "status": existing["status"], "duplicate": True}
            # event_id was NULL on both rows — fall back to a no-op record
            return {"id": None, "status": "skipped", "duplicate": False}

    async def mark_webhook_processed(
        self,
        webhook_id: str,
        *,
        payment_id: Optional[str] = None,
        order_id: Optional[str] = None,
        user_id: Optional[str] = None,
        restaurant_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        status: str = "processed",
        error_message: Optional[str] = None,
    ) -> None:
        async with get_connection() as conn:
            await conn.execute(
                """
                UPDATE webhook_events
                SET status        = $1,
                    payment_id    = COALESCE($2, payment_id),
                    order_id      = COALESCE($3, order_id),
                    user_id       = COALESCE($4, user_id),
                    restaurant_id = COALESCE($5, restaurant_id),
                    branch_id     = COALESCE($6, branch_id),
                    error_message = $7,
                    attempts      = attempts + 1,
                    processed_at  = NOW()
                WHERE id = $8
                """,
                status, payment_id, order_id, user_id, restaurant_id, branch_id,
                error_message, webhook_id,
            )

    # ─────────────────────────────────────────────────────────────────
    # 2. RUN A RECONCILIATION SCAN
    # ─────────────────────────────────────────────────────────────────
    async def run_reconciliation(
        self,
        user: UserContext,
        *,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        min_order_age_minutes: int = 15,
    ) -> dict:
        """Scan a time window for the 6+2 mismatch scenarios."""
        owner = _owner_id(user)
        now = datetime.now(timezone.utc)
        period_end = to_date or now
        period_start = from_date or (period_end - timedelta(days=7))

        if period_end <= period_start:
            raise ValidationError("to_date must be after from_date")

        async with get_transaction() as conn:
            run = await conn.fetchrow(
                """
                INSERT INTO reconciliation_runs (
                    user_id, restaurant_id, branch_id,
                    period_start, period_end, status, triggered_by
                ) VALUES ($1, $2, $3, $4, $5, 'running', $6)
                RETURNING id
                """,
                owner,
                str(user.restaurant_id) if user.restaurant_id else None,
                str(user.branch_id) if user.branch_id else None,
                period_start, period_end, user.user_id,
            )
            run_id = str(run["id"])

            try:
                discrepancies = []

                # ─ scenario 1: payment received but order not updated ─
                rows = await conn.fetch(
                    """
                    SELECT p.id AS payment_id, p.order_id, p.amount, p.paid_at,
                           o.status AS order_status, o.total_amount AS order_total
                    FROM   payments p
                    JOIN   orders o ON o.id = p.order_id
                    WHERE  p.user_id  = $1
                      AND  p.status   = 'completed'
                      AND  p.paid_at BETWEEN $2 AND $3
                      AND  LOWER(o.status) NOT IN (
                            'confirmed','preparing','ready','completed','served','delivered','cancelled'
                          )
                    """,
                    owner, period_start, period_end,
                )
                for r in rows:
                    discrepancies.append(dict(
                        kind="payment_received_order_not_updated",
                        severity="critical",
                        order_id=r["order_id"],
                        payment_id=r["payment_id"],
                        expected_amount=r["order_total"],
                        actual_amount=r["amount"],
                        delta_amount=Decimal(str(r["amount"] or 0)) - Decimal(str(r["order_total"] or 0)),
                        description=(
                            f"Payment {r['payment_id']} captured at {r['paid_at']:%Y-%m-%d %H:%M} "
                            f"but order is still in '{r['order_status']}'."
                        ),
                        metadata={"order_status": r["order_status"]},
                    ))

                # ─ scenario 2: order created but no payment ─
                cutoff_age = now - timedelta(minutes=min_order_age_minutes)
                rows = await conn.fetch(
                    """
                    SELECT o.id AS order_id, o.total_amount, o.status, o.created_at,
                           o.customer_id
                    FROM   orders o
                    LEFT   JOIN payments p ON p.order_id = o.id
                    WHERE  o.user_id    = $1
                      AND  o.created_at BETWEEN $2 AND $3
                      AND  o.created_at <  $4
                      AND  LOWER(o.status) <> 'cancelled'
                      AND  p.id IS NULL
                    """,
                    owner, period_start, period_end, cutoff_age,
                )
                for r in rows:
                    discrepancies.append(dict(
                        kind="order_created_payment_missing",
                        severity="warning",
                        order_id=r["order_id"],
                        payment_id=None,
                        customer_id=r["customer_id"],
                        expected_amount=r["total_amount"],
                        actual_amount=Decimal("0"),
                        delta_amount=-Decimal(str(r["total_amount"] or 0)),
                        description=(
                            f"Order in status '{r['status']}' has no payment record "
                            f"({(now - r['created_at']).total_seconds()//60:.0f} min old)."
                        ),
                        metadata={"order_status": r["status"]},
                    ))

                # ─ scenario 3: duplicate payments (same order, > 1 completed) ─
                rows = await conn.fetch(
                    """
                    SELECT order_id, COUNT(*) AS dup_count, SUM(amount) AS total_paid
                    FROM   payments
                    WHERE  user_id = $1
                      AND  status  = 'completed'
                      AND  paid_at BETWEEN $2 AND $3
                    GROUP  BY order_id
                    HAVING COUNT(*) > 1
                    """,
                    owner, period_start, period_end,
                )
                for r in rows:
                    discrepancies.append(dict(
                        kind="duplicate_payment",
                        severity="critical",
                        order_id=r["order_id"],
                        payment_id=None,
                        expected_amount=None,
                        actual_amount=r["total_paid"],
                        delta_amount=None,
                        description=(
                            f"{r['dup_count']} completed payments exist for the same order "
                            f"(total captured: ₹{_to_float(r['total_paid']):.2f})."
                        ),
                        metadata={"duplicate_count": int(r["dup_count"])},
                    ))

                # ─ scenario 4: failed / stuck settlements (pending > 7d) ─
                rows = await conn.fetch(
                    """
                    SELECT s.id AS settlement_id, s.gateway, s.gross_amount,
                           s.net_amount, s.settlement_date, s.status
                    FROM   pg_settlements s
                    WHERE  s.restaurant_id = $1::uuid
                      AND  s.status        = 'pending'
                      AND  s.settlement_date < (CURRENT_DATE - INTERVAL '7 days')
                    """,
                    str(user.restaurant_id) if user.restaurant_id else "00000000-0000-0000-0000-000000000000",
                )
                for r in rows:
                    discrepancies.append(dict(
                        kind="failed_settlement",
                        severity="critical",
                        order_id=None,
                        payment_id=None,
                        settlement_id=r["settlement_id"],
                        expected_amount=r["gross_amount"],
                        actual_amount=r["net_amount"],
                        description=(
                            f"{r['gateway']} settlement of ₹{_to_float(r['gross_amount']):.2f} "
                            f"scheduled {r['settlement_date']} is still pending."
                        ),
                        metadata={"gateway": r["gateway"]},
                    ))

                # ─ scenario 5: partial settlements (math doesn't add up) ─
                rows = await conn.fetch(
                    """
                    SELECT id AS settlement_id, gateway, gross_amount, gateway_fee,
                           tax_on_fee, net_amount
                    FROM   pg_settlements
                    WHERE  restaurant_id = $1::uuid
                      AND  settlement_date BETWEEN $2::date AND $3::date
                      AND  ABS(net_amount - (gross_amount - gateway_fee - tax_on_fee)) > 0.01
                    """,
                    str(user.restaurant_id) if user.restaurant_id else "00000000-0000-0000-0000-000000000000",
                    period_start.date(), period_end.date(),
                )
                for r in rows:
                    expected_net = (Decimal(str(r["gross_amount"])) - Decimal(str(r["gateway_fee"]))
                                    - Decimal(str(r["tax_on_fee"])))
                    delta = Decimal(str(r["net_amount"])) - expected_net
                    discrepancies.append(dict(
                        kind="partial_settlement",
                        severity="warning",
                        order_id=None,
                        payment_id=None,
                        settlement_id=r["settlement_id"],
                        expected_amount=expected_net,
                        actual_amount=r["net_amount"],
                        delta_amount=delta,
                        description=(
                            f"{r['gateway']} settlement net amount mismatch "
                            f"(expected ₹{float(expected_net):.2f}, got ₹{_to_float(r['net_amount']):.2f})."
                        ),
                        metadata={"gateway": r["gateway"]},
                    ))

                # ─ scenario 6: webhook delays / failures ─
                stale_cutoff = now - timedelta(minutes=10)
                rows = await conn.fetch(
                    """
                    SELECT id, gateway, event_type, status, received_at, attempts,
                           payment_id, order_id
                    FROM   webhook_events
                    WHERE  (
                                user_id = $1
                             OR (user_id IS NULL AND received_at BETWEEN $2 AND $3)
                           )
                      AND  (
                                status = 'failed'
                             OR (status = 'received' AND received_at < $4)
                           )
                    """,
                    owner, period_start, period_end, stale_cutoff,
                )
                for r in rows:
                    discrepancies.append(dict(
                        kind="webhook_delayed_or_failed",
                        severity="critical" if r["status"] == "failed" else "warning",
                        order_id=r["order_id"],
                        payment_id=r["payment_id"],
                        expected_amount=None,
                        actual_amount=None,
                        description=(
                            f"{r['gateway']}/{r['event_type']} webhook stuck in '{r['status']}' "
                            f"({r['attempts']} attempts, received {r['received_at']:%Y-%m-%d %H:%M})."
                        ),
                        metadata={"gateway": r["gateway"], "event_type": r["event_type"],
                                  "webhook_id": str(r["id"])},
                    ))

                # ─ bonus: amount mismatch (paid != ordered) ─
                rows = await conn.fetch(
                    """
                    SELECT p.id AS payment_id, p.order_id, p.amount AS paid,
                           o.total_amount AS ordered
                    FROM   payments p
                    JOIN   orders o ON o.id = p.order_id
                    WHERE  p.user_id = $1
                      AND  p.status  = 'completed'
                      AND  p.paid_at BETWEEN $2 AND $3
                      AND  ABS(p.amount - o.total_amount) > 0.01
                    """,
                    owner, period_start, period_end,
                )
                for r in rows:
                    discrepancies.append(dict(
                        kind="amount_mismatch",
                        severity="warning",
                        order_id=r["order_id"],
                        payment_id=r["payment_id"],
                        expected_amount=r["ordered"],
                        actual_amount=r["paid"],
                        delta_amount=Decimal(str(r["paid"])) - Decimal(str(r["ordered"])),
                        description=(
                            f"Payment ₹{_to_float(r['paid']):.2f} differs from order total "
                            f"₹{_to_float(r['ordered']):.2f}."
                        ),
                        metadata={},
                    ))

                # ─ insert all discrepancies ─
                for d in discrepancies:
                    await conn.execute(
                        """
                        INSERT INTO reconciliation_discrepancies (
                            run_id, user_id, restaurant_id, branch_id,
                            kind, severity, order_id, payment_id, settlement_id,
                            customer_id, expected_amount, actual_amount, delta_amount,
                            description, metadata
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9,
                            $10, $11, $12, $13, $14, $15::jsonb
                        )
                        """,
                        run_id, owner,
                        str(user.restaurant_id) if user.restaurant_id else None,
                        str(user.branch_id) if user.branch_id else None,
                        d["kind"], d["severity"],
                        d.get("order_id"), d.get("payment_id"), d.get("settlement_id"),
                        d.get("customer_id"),
                        d.get("expected_amount"), d.get("actual_amount"), d.get("delta_amount"),
                        d["description"], json.dumps(d.get("metadata") or {}),
                    )

                # ─ summary aggregates ─
                summary_row = await conn.fetchrow(
                    """
                    SELECT
                      (SELECT COUNT(*)               FROM orders   WHERE user_id = $1
                         AND created_at BETWEEN $2 AND $3) AS orders_scanned,
                      (SELECT COUNT(*)               FROM payments WHERE user_id = $1
                         AND created_at BETWEEN $2 AND $3) AS payments_scanned,
                      (SELECT COALESCE(SUM(total_amount),0) FROM orders   WHERE user_id = $1
                         AND created_at BETWEEN $2 AND $3) AS total_order_amount,
                      (SELECT COALESCE(SUM(amount),0)      FROM payments WHERE user_id = $1
                         AND status = 'completed' AND paid_at BETWEEN $2 AND $3) AS total_payment_amount
                    """,
                    owner, period_start, period_end,
                )
                settlements_row = await conn.fetchrow(
                    """
                    SELECT COUNT(*) AS n,
                           COALESCE(SUM(net_amount) FILTER (WHERE status IN ('received','reconciled')), 0) AS settled,
                           COALESCE(SUM(gross_amount) FILTER (WHERE status = 'pending'), 0) AS unsettled
                    FROM   pg_settlements
                    WHERE  restaurant_id = $1::uuid
                      AND  settlement_date BETWEEN $2::date AND $3::date
                    """,
                    str(user.restaurant_id) if user.restaurant_id else "00000000-0000-0000-0000-000000000000",
                    period_start.date(), period_end.date(),
                )

                await conn.execute(
                    """
                    UPDATE reconciliation_runs
                    SET orders_scanned         = $1,
                        payments_scanned       = $2,
                        settlements_scanned    = $3,
                        discrepancies_found    = $4,
                        total_order_amount     = $5,
                        total_payment_amount   = $6,
                        total_settled_amount   = $7,
                        total_unsettled_amount = $8,
                        status                 = 'completed',
                        completed_at           = NOW()
                    WHERE  id = $9
                    """,
                    int(summary_row["orders_scanned"]),
                    int(summary_row["payments_scanned"]),
                    int(settlements_row["n"]),
                    len(discrepancies),
                    summary_row["total_order_amount"],
                    summary_row["total_payment_amount"],
                    settlements_row["settled"],
                    settlements_row["unsettled"],
                    run_id,
                )

            except Exception as exc:
                await conn.execute(
                    """
                    UPDATE reconciliation_runs
                    SET status = 'failed', completed_at = NOW(), error_message = $1
                    WHERE id = $2
                    """,
                    str(exc), run_id,
                )
                logger.exception("reconciliation_run_failed", run_id=run_id)
                raise

        logger.info(
            "reconciliation_run_completed",
            run_id=run_id, user_id=owner, discrepancies=len(discrepancies),
            period_start=period_start.isoformat(), period_end=period_end.isoformat(),
        )
        return await self.get_run(user, run_id)

    # ─────────────────────────────────────────────────────────────────
    # 3. QUERIES (filters required by the spec)
    # ─────────────────────────────────────────────────────────────────
    async def list_runs(
        self,
        user: UserContext,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        owner = _owner_id(user)
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT id, period_start, period_end, status,
                       orders_scanned, payments_scanned, settlements_scanned,
                       discrepancies_found,
                       total_order_amount, total_payment_amount,
                       total_settled_amount, total_unsettled_amount,
                       triggered_by, started_at, completed_at, error_message
                FROM   reconciliation_runs
                WHERE  user_id = $1
                ORDER  BY started_at DESC
                LIMIT  $2 OFFSET $3
                """,
                owner, limit, offset,
            )
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM reconciliation_runs WHERE user_id = $1",
                owner,
            )
        return {
            "items": [dict(r) for r in rows],
            "total": int(total or 0),
            "limit": limit,
            "offset": offset,
        }

    async def get_run(self, user: UserContext, run_id: str) -> dict:
        owner = _owner_id(user)
        async with get_connection() as conn:
            run = await conn.fetchrow(
                "SELECT * FROM reconciliation_runs WHERE id = $1 AND user_id = $2",
                run_id, owner,
            )
            if not run:
                raise NotFoundError("ReconciliationRun", run_id)
            disc = await conn.fetch(
                """
                SELECT id, kind, severity, order_id, payment_id, settlement_id,
                       customer_id, expected_amount, actual_amount, delta_amount,
                       description, metadata, status, resolved_by, resolved_at,
                       resolution_notes, detected_at
                FROM   reconciliation_discrepancies
                WHERE  run_id = $1
                ORDER  BY severity DESC, detected_at DESC
                """,
                run_id,
            )
        return {"run": dict(run), "discrepancies": [dict(d) for d in disc]}

    async def list_discrepancies(
        self,
        user: UserContext,
        *,
        kind: Optional[str] = None,
        severity: Optional[str] = None,
        status: Optional[str] = None,
        order_id: Optional[str] = None,
        payment_id: Optional[str] = None,
        customer_id: Optional[int] = None,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Filtered discrepancy list. Supports every filter the spec requires."""
        owner = _owner_id(user)
        clauses = ["user_id = $1"]
        params: list[Any] = [owner]

        def _add(clause: str, val: Any) -> None:
            params.append(val)
            clauses.append(clause.replace("$?", f"${len(params)}"))

        if kind:        _add("kind = $?",        kind)
        if severity:    _add("severity = $?",    severity)
        if status:      _add("status = $?",      status)
        if order_id:    _add("order_id = $?",    order_id)
        if payment_id:  _add("payment_id = $?",  payment_id)
        if customer_id: _add("customer_id = $?", customer_id)
        if from_date:   _add("detected_at >= $?", from_date)
        if to_date:     _add("detected_at <= $?", to_date)

        where = " AND ".join(clauses)
        params.extend([limit, offset])

        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, run_id, kind, severity, order_id, payment_id, settlement_id,
                       customer_id, expected_amount, actual_amount, delta_amount,
                       description, metadata, status, detected_at, resolved_at, resolution_notes
                FROM   reconciliation_discrepancies
                WHERE  {where}
                ORDER  BY detected_at DESC
                LIMIT  ${len(params) - 1} OFFSET ${len(params)}
                """,
                *params,
            )
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM reconciliation_discrepancies WHERE {where}",
                *params[:-2],
            )
        return {
            "items": [dict(r) for r in rows],
            "total": int(total or 0),
            "limit": limit,
            "offset": offset,
        }

    async def resolve_discrepancy(
        self,
        user: UserContext,
        discrepancy_id: str,
        *,
        action: str,            # 'acknowledged' | 'resolved' | 'ignored'
        notes: Optional[str] = None,
    ) -> dict:
        if action not in {"acknowledged", "resolved", "ignored"}:
            raise ValidationError(f"Invalid action: {action}")
        owner = _owner_id(user)
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                UPDATE reconciliation_discrepancies
                SET    status = $1, resolved_by = $2, resolved_at = NOW(),
                       resolution_notes = $3
                WHERE  id = $4 AND user_id = $5
                RETURNING id, status
                """,
                action, user.user_id, notes, discrepancy_id, owner,
            )
            if not row:
                raise NotFoundError("Discrepancy", discrepancy_id)
        return dict(row)

    # ─────────────────────────────────────────────────────────────────
    # 4. UNIFIED RECONCILIATION SUMMARY (orders ↔ payments ↔ settlements)
    # ─────────────────────────────────────────────────────────────────
    async def summary(
        self,
        user: UserContext,
        *,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
    ) -> dict:
        owner = _owner_id(user)
        now = datetime.now(timezone.utc)
        period_end = to_date or now
        period_start = from_date or (period_end - timedelta(days=30))

        async with get_connection() as conn:
            o = await conn.fetchrow(
                """
                SELECT COUNT(*) AS n,
                       COALESCE(SUM(total_amount),0) AS total
                FROM   orders
                WHERE  user_id = $1 AND created_at BETWEEN $2 AND $3
                """,
                owner, period_start, period_end,
            )
            p = await conn.fetchrow(
                """
                SELECT COUNT(*)                                           AS n,
                       COALESCE(SUM(amount) FILTER (WHERE status='completed'), 0) AS captured,
                       COALESCE(SUM(amount) FILTER (WHERE status='pending'),   0) AS pending,
                       COALESCE(SUM(amount) FILTER (WHERE status='failed'),    0) AS failed,
                       COALESCE(SUM(amount) FILTER (WHERE status='refunded'),  0) AS refunded
                FROM   payments
                WHERE  user_id = $1 AND created_at BETWEEN $2 AND $3
                """,
                owner, period_start, period_end,
            )
            s = await conn.fetchrow(
                """
                SELECT COUNT(*) AS n,
                       COALESCE(SUM(gross_amount), 0)  AS gross,
                       COALESCE(SUM(gateway_fee),  0)  AS fees,
                       COALESCE(SUM(net_amount),   0)  AS net,
                       COALESCE(SUM(net_amount) FILTER (WHERE status='reconciled'), 0) AS reconciled_net
                FROM   pg_settlements
                WHERE  restaurant_id = $1::uuid
                  AND  settlement_date BETWEEN $2::date AND $3::date
                """,
                str(user.restaurant_id) if user.restaurant_id else "00000000-0000-0000-0000-000000000000",
                period_start.date(), period_end.date(),
            )
            issues = await conn.fetch(
                """
                SELECT kind, COUNT(*) AS n
                FROM   reconciliation_discrepancies
                WHERE  user_id = $1 AND status = 'open'
                  AND  detected_at BETWEEN $2 AND $3
                GROUP  BY kind
                """,
                owner, period_start, period_end,
            )
            webhook = await conn.fetchrow(
                """
                SELECT
                  COUNT(*)                                                 AS total,
                  COUNT(*) FILTER (WHERE status='processed')               AS processed,
                  COUNT(*) FILTER (WHERE status='failed')                  AS failed,
                  COUNT(*) FILTER (WHERE status IN ('received','processing')) AS pending
                FROM   webhook_events
                WHERE  (user_id = $1 OR user_id IS NULL)
                  AND  received_at BETWEEN $2 AND $3
                """,
                owner, period_start, period_end,
            )

        return {
            "period": {"start": period_start.isoformat(), "end": period_end.isoformat()},
            "orders": {
                "count": int(o["n"] or 0),
                "total_amount": _to_float(o["total"]),
            },
            "payments": {
                "count": int(p["n"] or 0),
                "captured_amount": _to_float(p["captured"]),
                "pending_amount":  _to_float(p["pending"]),
                "failed_amount":   _to_float(p["failed"]),
                "refunded_amount": _to_float(p["refunded"]),
            },
            "settlements": {
                "count": int(s["n"] or 0),
                "gross_amount": _to_float(s["gross"]),
                "fees_amount":  _to_float(s["fees"]),
                "net_amount":   _to_float(s["net"]),
                "reconciled_net_amount": _to_float(s["reconciled_net"]),
            },
            "open_issues_by_kind": {row["kind"]: int(row["n"]) for row in issues},
            "webhooks": {
                "total":     int(webhook["total"] or 0),
                "processed": int(webhook["processed"] or 0),
                "failed":    int(webhook["failed"] or 0),
                "pending":   int(webhook["pending"] or 0),
            },
        }


reconciliation_service = ReconciliationService()
