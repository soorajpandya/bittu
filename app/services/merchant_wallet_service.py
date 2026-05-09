"""
Merchant wallet & ledger service.

Provides the read-side of the merchant settlement infrastructure:

  * Wallet snapshot     — cash balance, online pending / settled, lifetime totals
  * Settlement history  — paginated list of bittu_settlements
  * Settlement detail   — header + transaction lines + timeline
  * Transaction ledger  — unified, paginated stream of every payment / settlement
                          / refund event for a restaurant
  * Daily closing       — per-day cash, online captured, online settled,
                          platform fees, GST, refunds — for accountant sign-off
  * Platform revenue    — Bittu's earnings (fees) + GST collected on those fees
  * GST report          — GST payable by Bittu for the period (output GST on fees)

All balances are derived directly from the immutable source-of-truth tables
(`payments`, `bittu_settlements`, `bittu_settlement_transactions`,
`bittu_settlement_timeline`) so they are always reproducible — never cached.

Tenant scoping uses `restaurant_id`; falls back to `_owner_id(user)` for
multi-restaurant owners.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from app.core.auth import UserContext
from app.core.database import get_connection
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.services.statement_service import (
    BITTU_FEE_RATE,
    GST_RATE,
    TOTAL_DEDUCTION_RATE,
    _calc_fee,
)

logger = get_logger(__name__)

# Cash-equivalent payment methods (kept aligned with reconciliation_service).
CASH_METHODS = {"cash", "counter", "cod"}


def _owner_id(user: UserContext) -> str:
    return user.owner_id if user.is_branch_user else user.user_id


def _f(value) -> float:
    """Decimal/None → float (2dp) for JSON serialisation."""
    if value is None:
        return 0.0
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def _restaurant_id(user: UserContext) -> str:
    if not user.restaurant_id:
        raise ValidationError(
            "This endpoint requires an active restaurant context."
        )
    return str(user.restaurant_id)


class MerchantWalletService:
    """Read-side aggregator for merchant balances and reports."""

    # ────────────────────────────────────────────────────────────────────
    # WALLET SNAPSHOT
    # ────────────────────────────────────────────────────────────────────
    async def wallet(
        self,
        user: UserContext,
        *,
        as_of_date: Optional[date] = None,
    ) -> dict:
        """
        Single-call snapshot of every balance an accountant cares about.
        All numbers are derived from immutable ledger sources.

        If `as_of_date` is provided, only records created on or before the end
        of that day (UTC) are included — useful for historical snapshots.

        PERF: All four aggregates are computed in ONE round trip via CTEs,
        and we avoid LOWER() on indexed columns so the planner can use
        `idx_payments_restaurant_method_status` and `idx_orders_restaurant_id`.
        """
        rid = _restaurant_id(user)

        # Build cutoff (end-of-day UTC) for historical snapshots.
        if as_of_date is not None:
            cutoff = datetime.combine(
                as_of_date, datetime.max.time()
            ).replace(tzinfo=timezone.utc)
            as_of_iso = cutoff.isoformat()
        else:
            cutoff = None
            as_of_iso = datetime.now(timezone.utc).isoformat()

        # Pre-compute case variants once — avoids LOWER() on the column,
        # which would otherwise prevent index use.
        cash_methods_variants: list[str] = []
        for m in CASH_METHODS:
            cash_methods_variants.extend({m, m.upper(), m.capitalize()})
        order_status_variants: list[str] = []
        for s in ("confirmed", "preparing", "ready", "completed", "served", "delivered"):
            order_status_variants.extend({s, s.upper(), s.capitalize()})

        # Cutoff clause is parameterised: when NULL the comparison is skipped.
        # Using `($2::timestamptz IS NULL OR created_at <= $2)` lets one
        # prepared plan serve both live and historical calls.
        sql = """
        WITH cash AS (
          SELECT
            COALESCE(SUM(amount) FILTER (WHERE status='completed'),0)::numeric(14,2) AS collected,
            COALESCE(SUM(amount) FILTER (WHERE status='refunded'), 0)::numeric(14,2) AS refunded,
            COUNT(*)              FILTER (WHERE status='completed')                  AS tx_count
          FROM payments
          WHERE restaurant_id = $1::uuid
            AND method = ANY($3::text[])
            AND ($2::timestamptz IS NULL OR created_at <= $2::timestamptz)
        ),
        online_captured AS (
          SELECT COALESCE(SUM(amount),0)::numeric(14,2) AS amount
          FROM payments
          WHERE restaurant_id = $1::uuid
            AND status = 'completed'
            AND NOT (method = ANY($3::text[]))
            AND ($2::timestamptz IS NULL OR created_at <= $2::timestamptz)
        ),
        settle AS (
          SELECT
            COALESCE(SUM(gross_amount)
              FILTER (WHERE settlement_status IN ('pending','processing','sent_to_bank'))
            ,0)::numeric(14,2) AS pending_gross,
            COALESCE(SUM(net_settlement_amount)
              FILTER (WHERE settlement_status IN ('pending','processing','sent_to_bank'))
            ,0)::numeric(14,2) AS pending_net,
            COALESCE(SUM(net_settlement_amount)
              FILTER (WHERE settlement_status='settled')
            ,0)::numeric(14,2) AS settled_lifetime,
            COALESCE(SUM(gross_amount)
              FILTER (WHERE settlement_status='settled')
            ,0)::numeric(14,2) AS settled_gross_lifetime,
            COALESCE(SUM(gross_amount)
              FILTER (WHERE settlement_status IN ('failed','reversed'))
            ,0)::numeric(14,2) AS failed_or_reversed,
            COALESCE(SUM(bittu_fee_amount)
              FILTER (WHERE settlement_status='settled')
            ,0)::numeric(14,2) AS platform_fee_paid,
            COALESCE(SUM(gst_amount)
              FILTER (WHERE settlement_status='settled')
            ,0)::numeric(14,2) AS gst_on_fee_paid,
            COUNT(*) FILTER (WHERE settlement_status='settled')                       AS settled_count,
            COUNT(*) FILTER (WHERE settlement_status IN ('pending','processing','sent_to_bank')) AS pending_count
          FROM bittu_settlements
          WHERE restaurant_id = $1::uuid
            AND ($2::timestamptz IS NULL OR created_at <= $2::timestamptz)
        ),
        ords AS (
          SELECT
            COUNT(*)                                       AS total_orders,
            COALESCE(SUM(total_amount),0)::numeric(14,2)   AS total_sales
          FROM orders
          WHERE restaurant_id = $1::uuid
            AND status = ANY($4::text[])
            AND ($2::timestamptz IS NULL OR created_at <= $2::timestamptz)
        )
        SELECT
          cash.collected, cash.refunded, cash.tx_count,
          online_captured.amount AS online_captured,
          settle.pending_gross, settle.pending_net,
          settle.settled_lifetime, settle.settled_gross_lifetime,
          settle.failed_or_reversed, settle.platform_fee_paid, settle.gst_on_fee_paid,
          settle.settled_count, settle.pending_count,
          ords.total_orders, ords.total_sales
        FROM cash, online_captured, settle, ords
        """

        async with get_connection() as conn:
            row = await conn.fetchrow(
                sql,
                rid,
                cutoff,
                cash_methods_variants,
                order_status_variants,
            )

        # Map row → previous local-variable shape so the response block
        # below stays unchanged.
        cash    = {"collected": row["collected"], "refunded": row["refunded"], "tx_count": row["tx_count"]}
        online_captured = row["online_captured"]
        online  = {
            "pending_gross":          row["pending_gross"],
            "pending_net":            row["pending_net"],
            "settled_lifetime":       row["settled_lifetime"],
            "settled_gross_lifetime": row["settled_gross_lifetime"],
            "failed_or_reversed":     row["failed_or_reversed"],
            "platform_fee_paid":      row["platform_fee_paid"],
            "gst_on_fee_paid":        row["gst_on_fee_paid"],
            "settled_count":          row["settled_count"],
            "pending_count":          row["pending_count"],
        }
        orders  = {"total_orders": row["total_orders"], "total_sales": row["total_sales"]}

        cash_collected = _f(cash["collected"])
        cash_refunded  = _f(cash["refunded"])
        cash_balance   = round(cash_collected - cash_refunded, 2)

        online_captured_f      = _f(online_captured)
        online_pending_net_f   = _f(online["pending_net"])
        online_settled_f       = _f(online["settled_lifetime"])
        online_failed_f        = _f(online["failed_or_reversed"])

        return {
            "restaurant_id": rid,
            "as_of":         as_of_iso,
            "as_of_date":    as_of_date.isoformat() if as_of_date else None,
            # ─ Top-line activity ──────────────────────────────────────
            "sales": {
                "total_orders":   int(orders["total_orders"] or 0),
                "total_sales":    _f(orders["total_sales"]),
                "cash_sales":     cash_collected,
                "online_sales":   online_captured_f,
            },
            # ─ Cash wallet (no gateway, no settlement) ────────────────
            "cash_wallet": {
                "balance":            cash_balance,
                "collected_lifetime": cash_collected,
                "refunded_lifetime":  cash_refunded,
                "tx_count":           int(cash["tx_count"] or 0),
                "note": "Cash never auto-settles. Subtract physical deposits to reconcile.",
            },
            # ─ Online wallet (gateway → bank via settlements) ─────────
            "online_wallet": {
                "pending_settlement_gross": _f(online["pending_gross"]),
                "pending_settlement_net":   online_pending_net_f,
                "settled_lifetime_net":     online_settled_f,
                "settled_lifetime_gross":   _f(online["settled_gross_lifetime"]),
                "failed_or_reversed":       online_failed_f,
                "captured_lifetime":        online_captured_f,
                "in_clearing":              round(
                    online_captured_f
                    - _f(online["settled_gross_lifetime"])
                    - _f(online["pending_gross"])
                    - online_failed_f,
                    2,
                ),
                "settled_count":            int(online["settled_count"] or 0),
                "pending_count":            int(online["pending_count"] or 0),
            },
            # ─ Platform side (Bittu's revenue) ────────────────────────
            "platform_revenue": {
                "fee_collected_lifetime": _f(online["platform_fee_paid"]),
                "gst_on_fee_lifetime":    _f(online["gst_on_fee_paid"]),
                "total_deduction_pct":    "0.30%",
                "fee_pct":                "0.2542%",
                "gst_pct_on_fee":         "18%",
            },
        }

    # ────────────────────────────────────────────────────────────────────
    # FEE CALCULATOR  (preview before payment)
    # ────────────────────────────────────────────────────────────────────
    async def quote_fee(self, gross_amount: float, method: str = "upi") -> dict:
        """
        Pure-function preview of what would be deducted for a transaction
        of `gross_amount`.  Cash → no deduction.  Online → 0.30 % split.
        """
        if gross_amount <= 0:
            raise ValidationError("gross_amount must be positive")

        if method.lower() in CASH_METHODS:
            return {
                "method":            method,
                "channel":           "cash",
                "gross_amount":      _f(gross_amount),
                "platform_fee":      0.0,
                "gst_on_fee":        0.0,
                "total_deduction":   0.0,
                "net_settlement":    _f(gross_amount),
                "settlement_eta":    None,
                "note":              "Cash bypasses gateway and settlement.",
            }

        gross = Decimal(str(gross_amount))
        fee, gst, net = _calc_fee(gross)
        return {
            "method":            method,
            "channel":           "online",
            "gross_amount":      _f(gross),
            "platform_fee":      _f(fee),
            "gst_on_fee":        _f(gst),
            "total_deduction":   _f(fee + gst),
            "net_settlement":    _f(net),
            "fee_rate_pct":      "0.2542%",
            "gst_rate_pct":      "18%",
            "total_rate_pct":    "0.30%",
            "settlement_eta":    "T+1 (next working day)",
        }

    # ────────────────────────────────────────────────────────────────────
    # SETTLEMENT HISTORY
    # ────────────────────────────────────────────────────────────────────
    async def list_settlements(
        self,
        user: UserContext,
        *,
        status: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        rid = _restaurant_id(user)
        clauses = ["restaurant_id = $1::uuid"]
        params: list[Any] = [rid]

        def _add(clause: str, val: Any) -> None:
            params.append(val)
            clauses.append(clause.replace("$?", f"${len(params)}"))

        if status:    _add("settlement_status = $?", status)
        if from_date: _add("created_at >= $?",       datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc))
        if to_date:   _add("created_at <= $?",       datetime.combine(to_date, datetime.max.time(), tzinfo=timezone.utc))

        where = " AND ".join(clauses)
        params.extend([limit, offset])
        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, settlement_reference, settlement_status, settlement_cycle,
                       gross_amount, bittu_fee_amount, gst_amount, net_settlement_amount,
                       expected_settlement_at, settled_at, bank_reference_number,
                       failure_reason, retry_count, created_at, updated_at
                FROM   bittu_settlements
                WHERE  {where}
                ORDER  BY created_at DESC
                LIMIT  ${len(params) - 1} OFFSET ${len(params)}
                """,
                *params,
            )
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM bittu_settlements WHERE {where}",
                *params[:-2],
            )

        return {
            "total":  int(total or 0),
            "limit":  limit,
            "offset": offset,
            "items": [
                {
                    "id":                       str(r["id"]),
                    "reference":                r["settlement_reference"],
                    "status":                   r["settlement_status"],
                    "cycle":                    r["settlement_cycle"],
                    "gross_amount":             _f(r["gross_amount"]),
                    "platform_fee":             _f(r["bittu_fee_amount"]),
                    "gst_on_fee":               _f(r["gst_amount"]),
                    "net_settlement":           _f(r["net_settlement_amount"]),
                    "expected_settlement_at":   r["expected_settlement_at"].isoformat() if r["expected_settlement_at"] else None,
                    "settled_at":               r["settled_at"].isoformat() if r["settled_at"] else None,
                    "bank_reference":           r["bank_reference_number"],
                    "failure_reason":           r["failure_reason"],
                    "retry_count":              int(r["retry_count"] or 0),
                    "created_at":               r["created_at"].isoformat(),
                    "updated_at":               r["updated_at"].isoformat(),
                }
                for r in rows
            ],
        }

    async def get_settlement(self, user: UserContext, settlement_id: str) -> dict:
        rid = _restaurant_id(user)
        async with get_connection() as conn:
            head = await conn.fetchrow(
                """
                SELECT * FROM bittu_settlements
                WHERE  id = $1::uuid AND restaurant_id = $2::uuid
                """,
                settlement_id, rid,
            )
            if not head:
                raise NotFoundError(f"Settlement {settlement_id} not found")

            tx_rows = await conn.fetch(
                """
                SELECT id, payment_id, order_id, gross_amount, fee_amount, gst_amount,
                       net_amount, transaction_type, payment_method, customer_name,
                       order_reference, settlement_status, created_at
                FROM   bittu_settlement_transactions
                WHERE  settlement_id = $1::uuid
                ORDER  BY created_at ASC
                """,
                settlement_id,
            )
            timeline = await conn.fetch(
                """
                SELECT event_type, title, description, from_status, to_status,
                       actor_id, actor_type, metadata, occurred_at
                FROM   bittu_settlement_timeline
                WHERE  settlement_id = $1::uuid
                ORDER  BY occurred_at ASC
                """,
                settlement_id,
            )

        return {
            "id":                     str(head["id"]),
            "reference":              head["settlement_reference"],
            "status":                 head["settlement_status"],
            "cycle":                  head["settlement_cycle"],
            "gross_amount":           _f(head["gross_amount"]),
            "platform_fee":           _f(head["bittu_fee_amount"]),
            "gst_on_fee":             _f(head["gst_amount"]),
            "net_settlement":         _f(head["net_settlement_amount"]),
            "fee_rate":               float(head["fee_rate"]),
            "gst_rate":               float(head["gst_rate"]),
            "expected_settlement_at": head["expected_settlement_at"].isoformat() if head["expected_settlement_at"] else None,
            "settled_at":             head["settled_at"].isoformat() if head["settled_at"] else None,
            "bank_reference":         head["bank_reference_number"],
            "failure_reason":         head["failure_reason"],
            "retry_count":            int(head["retry_count"] or 0),
            "created_at":             head["created_at"].isoformat(),
            "updated_at":             head["updated_at"].isoformat(),
            "transactions": [
                {
                    "id":             str(t["id"]),
                    "payment_id":     str(t["payment_id"]) if t["payment_id"] else None,
                    "order_id":       str(t["order_id"]) if t["order_id"] else None,
                    "gross":          _f(t["gross_amount"]),
                    "fee":            _f(t["fee_amount"]),
                    "gst":            _f(t["gst_amount"]),
                    "net":            _f(t["net_amount"]),
                    "type":           t["transaction_type"],
                    "method":         t["payment_method"],
                    "customer":       t["customer_name"],
                    "order_ref":      t["order_reference"],
                    "status":         t["settlement_status"],
                    "created_at":     t["created_at"].isoformat(),
                }
                for t in tx_rows
            ],
            "timeline": [
                {
                    "event":       e["event_type"],
                    "title":       e["title"],
                    "description": e["description"],
                    "from":        e["from_status"],
                    "to":          e["to_status"],
                    "actor_id":    e["actor_id"],
                    "actor_type":  e["actor_type"],
                    "metadata":    dict(e["metadata"] or {}),
                    "occurred_at": e["occurred_at"].isoformat(),
                }
                for e in timeline
            ],
        }

    # ────────────────────────────────────────────────────────────────────
    # TRANSACTION LEDGER  (unified stream)
    # ────────────────────────────────────────────────────────────────────
    async def list_transactions(
        self,
        user: UserContext,
        *,
        method: Optional[str] = None,         # 'cash' | 'online' | specific method
        status: Optional[str] = None,         # payment status
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """
        Unified transaction ledger:  every payment row joined with its
        settlement (if any) so the caller can see lifecycle in one row.
        """
        rid = _restaurant_id(user)
        clauses = ["p.restaurant_id = $1::uuid"]
        params: list[Any] = [rid]

        def _add(clause: str, val: Any) -> None:
            params.append(val)
            clauses.append(clause.replace("$?", f"${len(params)}"))

        cash_methods_sql = "(" + ",".join(f"'{m}'" for m in CASH_METHODS) + ")"
        if method == "cash":
            clauses.append(f"LOWER(p.method) IN {cash_methods_sql}")
        elif method == "online":
            clauses.append(f"LOWER(p.method) NOT IN {cash_methods_sql}")
        elif method:
            _add("LOWER(p.method) = $?", method.lower())

        if status:    _add("p.status = $?",     status)
        if from_date: _add("p.created_at >= $?", datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc))
        if to_date:   _add("p.created_at <= $?", datetime.combine(to_date, datetime.max.time(), tzinfo=timezone.utc))

        where = " AND ".join(clauses)
        params.extend([limit, offset])
        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""
                SELECT p.id, p.order_id, p.method, p.status, p.amount,
                       p.razorpay_payment_id, p.paid_at, p.created_at,
                       bst.id            AS settlement_tx_id,
                       bst.settlement_id AS settlement_id,
                       bst.fee_amount    AS settlement_fee,
                       bst.gst_amount    AS settlement_gst,
                       bst.net_amount    AS settlement_net,
                       bst.settlement_status,
                       o.total_amount    AS order_total,
                       o.status          AS order_status
                FROM   payments p
                LEFT   JOIN bittu_settlement_transactions bst
                       ON bst.payment_id = p.id AND bst.transaction_type = 'payment'
                LEFT   JOIN orders o ON o.id = p.order_id
                WHERE  {where}
                ORDER  BY p.created_at DESC
                LIMIT  ${len(params) - 1} OFFSET ${len(params)}
                """,
                *params,
            )
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM payments p WHERE {where}",
                *params[:-2],
            )

        return {
            "total":  int(total or 0),
            "limit":  limit,
            "offset": offset,
            "items": [
                {
                    "payment_id":         str(r["id"]),
                    "order_id":           str(r["order_id"]) if r["order_id"] else None,
                    "order_total":        _f(r["order_total"]),
                    "order_status":       r["order_status"],
                    "method":             r["method"],
                    "channel":            "cash" if (r["method"] or "").lower() in CASH_METHODS else "online",
                    "status":             r["status"],
                    "amount":             _f(r["amount"]),
                    "gateway_payment_id": r["razorpay_payment_id"],
                    "paid_at":            r["paid_at"].isoformat() if r["paid_at"] else None,
                    "created_at":         r["created_at"].isoformat(),
                    "settlement": {
                        "id":            str(r["settlement_id"]) if r["settlement_id"] else None,
                        "tx_id":         str(r["settlement_tx_id"]) if r["settlement_tx_id"] else None,
                        "fee":           _f(r["settlement_fee"]),
                        "gst":           _f(r["settlement_gst"]),
                        "net":           _f(r["settlement_net"]),
                        "status":        r["settlement_status"],
                    } if r["settlement_id"] else None,
                }
                for r in rows
            ],
        }

    # ────────────────────────────────────────────────────────────────────
    # DAILY CLOSING REPORT
    # ────────────────────────────────────────────────────────────────────
    async def daily_closing(
        self,
        user: UserContext,
        closing_date: Optional[date] = None,
    ) -> dict:
        """One-day close: cash collected, online captured, online settled,
        platform fees, GST, refunds — accountant sign-off view."""
        rid = _restaurant_id(user)
        d   = closing_date or date.today()
        day_start = datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)
        day_end   = datetime.combine(d, datetime.max.time(), tzinfo=timezone.utc)
        cash_methods_sql = "(" + ",".join(f"'{m}'" for m in CASH_METHODS) + ")"

        async with get_connection() as conn:
            pay = await conn.fetchrow(
                f"""
                SELECT
                  COALESCE(SUM(amount) FILTER (
                    WHERE status='completed' AND LOWER(method) IN {cash_methods_sql}
                  ),0)::numeric(14,2)                                  AS cash_collected,
                  COALESCE(SUM(amount) FILTER (
                    WHERE status='completed' AND LOWER(method) NOT IN {cash_methods_sql}
                  ),0)::numeric(14,2)                                  AS online_captured,
                  COALESCE(SUM(amount) FILTER (WHERE status='refunded'),0)::numeric(14,2)
                                                                        AS refunds,
                  COUNT(*) FILTER (WHERE status='completed')             AS tx_count
                FROM payments
                WHERE restaurant_id = $1::uuid
                  AND created_at BETWEEN $2 AND $3
                """,
                rid, day_start, day_end,
            )
            settled = await conn.fetchrow(
                """
                SELECT
                  COALESCE(SUM(net_settlement_amount) FILTER (WHERE settlement_status='settled'),0)::numeric(14,2) AS settled_net,
                  COALESCE(SUM(gross_amount)          FILTER (WHERE settlement_status='settled'),0)::numeric(14,2) AS settled_gross,
                  COALESCE(SUM(bittu_fee_amount)      FILTER (WHERE settlement_status='settled'),0)::numeric(14,2) AS fees,
                  COALESCE(SUM(gst_amount)            FILTER (WHERE settlement_status='settled'),0)::numeric(14,2) AS gst,
                  COALESCE(SUM(gross_amount)          FILTER (WHERE settlement_status IN ('failed','reversed')),0)::numeric(14,2) AS failed,
                  COUNT(*)                            FILTER (WHERE settlement_status='settled') AS settled_count
                FROM bittu_settlements
                WHERE restaurant_id = $1::uuid
                  AND COALESCE(settled_at::date, created_at::date) = $2
                """,
                rid, d,
            )

        return {
            "restaurant_id":  rid,
            "closing_date":   d.isoformat(),
            "cash": {
                "collected": _f(pay["cash_collected"]),
                "note":      "Match against the cash drawer count.",
            },
            "online": {
                "captured":    _f(pay["online_captured"]),
                "settled_net": _f(settled["settled_net"]),
                "settled_gross": _f(settled["settled_gross"]),
                "in_clearing": round(_f(pay["online_captured"]) - _f(settled["settled_gross"]), 2),
                "failed_or_reversed": _f(settled["failed"]),
            },
            "platform_revenue": {
                "fee_collected": _f(settled["fees"]),
                "gst_on_fee":    _f(settled["gst"]),
            },
            "refunds_total":  _f(pay["refunds"]),
            "tx_count":       int(pay["tx_count"] or 0),
            "settled_count":  int(settled["settled_count"] or 0),
            "totals": {
                "gross_revenue":         round(_f(pay["cash_collected"]) + _f(pay["online_captured"]), 2),
                "net_to_merchant_today": round(
                    _f(pay["cash_collected"]) + _f(settled["settled_net"]),
                    2,
                ),
            },
        }

    # ────────────────────────────────────────────────────────────────────
    # PLATFORM REVENUE REPORT
    # ────────────────────────────────────────────────────────────────────
    async def platform_revenue_report(
        self,
        user: UserContext,
        from_date: Optional[date] = None,
        to_date:   Optional[date] = None,
    ) -> dict:
        """
        Bittu's revenue from this merchant for the period:
          * platform fees collected (settled only)
          * GST collected on those fees (output GST liability for Bittu)
          * effective rate vs gross
        """
        rid = _restaurant_id(user)
        end   = to_date   or date.today()
        start = from_date or (end - timedelta(days=30))

        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                  COALESCE(SUM(gross_amount),0)::numeric(14,2)          AS gross_settled,
                  COALESCE(SUM(net_settlement_amount),0)::numeric(14,2) AS net_paid_to_merchant,
                  COALESCE(SUM(bittu_fee_amount),0)::numeric(14,2)      AS fee_collected,
                  COALESCE(SUM(gst_amount),0)::numeric(14,2)            AS gst_collected,
                  COUNT(*)                                              AS settlement_count
                FROM bittu_settlements
                WHERE restaurant_id = $1::uuid
                  AND settlement_status = 'settled'
                  AND COALESCE(settled_at::date, created_at::date) BETWEEN $2 AND $3
                """,
                rid, start, end,
            )
        gross = _f(row["gross_settled"])
        fee   = _f(row["fee_collected"])
        gst   = _f(row["gst_collected"])
        return {
            "period": {"from": start.isoformat(), "to": end.isoformat()},
            "gross_settled":         gross,
            "net_paid_to_merchant":  _f(row["net_paid_to_merchant"]),
            "platform_fee_revenue":  fee,
            "gst_on_fee_collected":  gst,
            "total_deduction":       round(fee + gst, 2),
            "effective_rate_pct":    round(((fee + gst) / gross) * 100, 4) if gross > 0 else 0.0,
            "settlement_count":      int(row["settlement_count"] or 0),
            "rate_card": {
                "fee_pct":       "0.2542%",
                "gst_pct":       "18% (on fee)",
                "total_pct":     "0.30% (on gross)",
            },
        }

    # ────────────────────────────────────────────────────────────────────
    # GST REPORT (output GST on platform fees collected)
    # ────────────────────────────────────────────────────────────────────
    async def fee_gst_report(
        self,
        user: UserContext,
        from_date: Optional[date] = None,
        to_date:   Optional[date] = None,
    ) -> dict:
        """
        GST that Bittu has collected from this merchant via the platform
        fee.  This is Bittu's output GST liability for the period.
        Restaurants need this for their input-credit reconciliation.
        """
        rid = _restaurant_id(user)
        end   = to_date   or date.today()
        start = from_date or end.replace(day=1)

        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT
                  COALESCE(settled_at::date, created_at::date) AS d,
                  COALESCE(SUM(gross_amount),0)::numeric(14,2)    AS gross,
                  COALESCE(SUM(bittu_fee_amount),0)::numeric(14,2) AS fee,
                  COALESCE(SUM(gst_amount),0)::numeric(14,2)      AS gst
                FROM bittu_settlements
                WHERE restaurant_id = $1::uuid
                  AND settlement_status = 'settled'
                  AND COALESCE(settled_at::date, created_at::date) BETWEEN $2 AND $3
                GROUP BY 1
                ORDER BY 1
                """,
                rid, start, end,
            )
        days = [
            {
                "date":  r["d"].isoformat(),
                "gross": _f(r["gross"]),
                "fee":   _f(r["fee"]),
                "gst":   _f(r["gst"]),
            }
            for r in rows
        ]
        return {
            "period":             {"from": start.isoformat(), "to": end.isoformat()},
            "total_fee":          round(sum(d["fee"] for d in days), 2),
            "total_gst_on_fee":   round(sum(d["gst"] for d in days), 2),
            "total_gross_settled": round(sum(d["gross"] for d in days), 2),
            "by_day":             days,
            "note": (
                "GST shown here is collected by Bittu on its platform fee "
                "(SAC: 998314, Information Technology Software Services). "
                "Merchants can claim this as input tax credit (ITC) "
                "subject to GSTR-2B reconciliation."
            ),
        }


merchant_wallet_service = MerchantWalletService()
