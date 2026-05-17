"""
Phase 8 — Reporting & Analytics service.

Aggregates read-only data from existing source tables and the
`merchant_daily_rollups` cache (migration 044). No gateway wiring,
no mutations of source data — this service is strictly an aggregator.

Scoping rules (enforced by callers):
  • Merchant routers must always pass `merchant_id` (== UserContext.restaurant_id).
  • Admin routers may omit `merchant_id` to get cross-merchant totals,
    or pass one to filter to a single merchant.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.core.database import get_connection

log = logging.getLogger(__name__)


def _d(v: Any) -> Decimal:
    return Decimal(str(v if v is not None else 0))


def _row_to_rollup(row) -> dict:
    return {
        "id": row["id"],
        "merchant_id": str(row["merchant_id"]),
        "rollup_date": row["rollup_date"].isoformat(),
        "currency": row["currency"],
        "orders_count": row["orders_count"],
        "orders_completed_count": row["orders_completed_count"],
        "gross_sales": _d(row["gross_sales"]),
        "discounts_total": _d(row["discounts_total"]),
        "tax_total": _d(row["tax_total"]),
        "cogs_total": _d(row["cogs_total"]),
        "payments_count": row["payments_count"],
        "payments_amount": _d(row["payments_amount"]),
        "payments_cash_amount": _d(row["payments_cash_amount"]),
        "refunds_count": row["refunds_count"],
        "refunds_initiated_amount": _d(row["refunds_initiated_amount"]),
        "refunds_succeeded_amount": _d(row["refunds_succeeded_amount"]),
        "refunds_failed_count": row["refunds_failed_count"],
        "disputes_opened_count": row["disputes_opened_count"],
        "disputes_lost_amount": _d(row["disputes_lost_amount"]),
        "disputes_won_count": row["disputes_won_count"],
        "ledger_debit": _d(row["ledger_debit"]),
        "ledger_credit": _d(row["ledger_credit"]),
        "ledger_net": _d(row["ledger_net"]),
        "fees_total": _d(row["fees_total"]),
        "gst_total": _d(row["gst_total"]),
        "chargebacks_total": _d(row["chargebacks_total"]),
        "computed_at": row["computed_at"].isoformat() if row["computed_at"] else None,
        "computed_by": str(row["computed_by"]) if row["computed_by"] else None,
        "source_version": row["source_version"],
    }


class ReportingService:
    # ────────────────────────────────────────────────────────────────
    # P&L (on-the-fly aggregation across source tables)
    # ────────────────────────────────────────────────────────────────
    async def pnl(
        self,
        *,
        merchant_id: str | UUID | None,
        from_date: date,
        to_date: date,
        currency: str = "INR",
    ) -> dict:
        # Build param lists per-query so asyncpg's placeholder count matches
        # exactly the placeholders in each prepared statement.
        if merchant_id:
            o_params = [str(merchant_id), from_date, to_date]
            o_scope, o_pf, o_pt = "AND restaurant_id = $1::uuid", "$2", "$3"
            m_params = [str(merchant_id), from_date, to_date, currency]
            m_scope, m_pf, m_pt, m_pc = "AND merchant_id = $1::uuid", "$2", "$3", "$4"
        else:
            o_params = [from_date, to_date]
            o_scope, o_pf, o_pt = "", "$1", "$2"
            m_params = [from_date, to_date, currency]
            m_scope, m_pf, m_pt, m_pc = "", "$1", "$2", "$3"

        async with get_connection() as c:
            # Non-revenue order statuses (cancelled QRs, failed/expired/refunded,
            # unpaid pending_payment) must NOT inflate gross_sales, orders_count
            # or AOV. Mirrors the frontend filter shipped in c1dc17d.
            from app.core.order_status import NON_REVENUE_ORDER_STATUSES
            _nr = ",".join(f"'{s}'" for s in sorted(NON_REVENUE_ORDER_STATUSES))
            orders = await c.fetchrow(
                f"""
                SELECT
                    COUNT(*)                                            AS orders_count,
                    COALESCE(SUM(total_amount), 0)                      AS gross_sales,
                    COALESCE(SUM(discount_amount), 0)                   AS discounts,
                    COALESCE(SUM(tax_amount), 0)                        AS tax,
                    COALESCE(SUM(cost_of_goods_sold), 0)                AS cogs
                FROM orders
                WHERE created_at >= {o_pf}::date
                  AND created_at <  ({o_pt}::date + INTERVAL '1 day')
                  AND LOWER(status) NOT IN ({_nr})
                  {o_scope}
                """,
                *o_params,
            )
            refunds = await c.fetchrow(
                f"""
                SELECT
                    COUNT(*)                                                       AS refunds_count,
                    COALESCE(SUM(amount) FILTER (WHERE status = 'succeeded'), 0)   AS refunds_amount
                FROM refunds
                WHERE currency = {m_pc}
                  AND created_at >= {m_pf}::date
                  AND created_at <  ({m_pt}::date + INTERVAL '1 day')
                  {m_scope}
                """,
                *m_params,
            )
            disputes = await c.fetchrow(
                f"""
                SELECT
                    COUNT(*)                                                AS disputes_count,
                    COALESCE(SUM(amount) FILTER (WHERE status = 'lost'), 0) AS chargebacks_amount
                FROM disputes
                WHERE currency = {m_pc}
                  AND opened_at >= {m_pf}::date
                  AND opened_at <  ({m_pt}::date + INTERVAL '1 day')
                  {m_scope}
                """,
                *m_params,
            )
            ledger = await c.fetchrow(
                f"""
                SELECT
                    COALESCE(SUM(debit_amount)  FILTER (WHERE transaction_type = 'fee_deduction'), 0) AS fees,
                    COALESCE(SUM(debit_amount)  FILTER (WHERE transaction_type = 'gst_deduction'), 0) AS gst,
                    COALESCE(SUM(credit_amount) FILTER (WHERE transaction_type = 'payment_received'), 0) AS payments_in,
                    COALESCE(SUM(debit_amount)  FILTER (WHERE transaction_type = 'settlement_completed'), 0) AS settlements_out
                FROM merchant_ledger
                WHERE currency = {m_pc}
                  AND created_at >= {m_pf}::date
                  AND created_at <  ({m_pt}::date + INTERVAL '1 day')
                  {m_scope}
                """,
                *m_params,
            )

        gross   = _d(orders["gross_sales"])
        refnd   = _d(refunds["refunds_amount"])
        chgbck  = _d(disputes["chargebacks_amount"])
        fees    = _d(ledger["fees"])
        gst     = _d(ledger["gst"])
        net     = gross - refnd - chgbck - fees - gst

        return {
            "merchant_id": str(merchant_id) if merchant_id else None,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "currency": currency,
            "orders_count": orders["orders_count"],
            "gross_sales": gross,
            "discounts": _d(orders["discounts"]),
            "tax": _d(orders["tax"]),
            "cogs": _d(orders["cogs"]),
            "refunds_count": refunds["refunds_count"],
            "refunds_amount": refnd,
            "disputes_count": disputes["disputes_count"],
            "chargebacks_amount": chgbck,
            "fees": fees,
            "gst_on_fees": gst,
            "payments_in": _d(ledger["payments_in"]),
            "settlements_out": _d(ledger["settlements_out"]),
            "net_position": net,
        }

    # ────────────────────────────────────────────────────────────────
    # Settlement summary (bittu_settlements)
    # ────────────────────────────────────────────────────────────────
    async def settlement_summary(
        self,
        *,
        merchant_id: str | UUID | None,
        from_date: date,
        to_date: date,
    ) -> dict:
        scope = "AND restaurant_id = $1::uuid" if merchant_id else ""
        params: list[Any] = []
        if merchant_id:
            params.append(str(merchant_id))
        params.extend([from_date, to_date])
        p_from = f"${len(params) - 1}"
        p_to   = f"${len(params)}"

        async with get_connection() as c:
            row = await c.fetchrow(
                f"""
                SELECT
                    COUNT(*)                                                          AS total_count,
                    COUNT(*) FILTER (WHERE settlement_status = 'settled')             AS settled_count,
                    COUNT(*) FILTER (WHERE settlement_status = 'pending')             AS pending_count,
                    COUNT(*) FILTER (WHERE settlement_status = 'failed')              AS failed_count,
                    COUNT(*) FILTER (WHERE settlement_status = 'reversed')            AS reversed_count,
                    COALESCE(SUM(gross_amount), 0)                                    AS gross_total,
                    COALESCE(SUM(bittu_fee_amount), 0)                                AS fee_total,
                    COALESCE(SUM(gst_amount), 0)                                      AS gst_total,
                    COALESCE(SUM(net_settlement_amount), 0)                           AS net_total,
                    COALESCE(SUM(net_settlement_amount) FILTER (WHERE settlement_status = 'settled'), 0) AS settled_net
                FROM bittu_settlements
                WHERE created_at >= {p_from}::date
                  AND created_at <  ({p_to}::date + INTERVAL '1 day')
                  {scope}
                """,
                *params,
            )

        return {
            "merchant_id": str(merchant_id) if merchant_id else None,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "total_count": row["total_count"],
            "settled_count": row["settled_count"],
            "pending_count": row["pending_count"],
            "failed_count": row["failed_count"],
            "reversed_count": row["reversed_count"],
            "gross_total": _d(row["gross_total"]),
            "fee_total": _d(row["fee_total"]),
            "gst_total": _d(row["gst_total"]),
            "net_total": _d(row["net_total"]),
            "settled_net": _d(row["settled_net"]),
        }

    # ────────────────────────────────────────────────────────────────
    # Refund summary
    # ────────────────────────────────────────────────────────────────
    async def refund_summary(
        self,
        *,
        merchant_id: str | UUID | None,
        from_date: date,
        to_date: date,
        currency: str = "INR",
    ) -> dict:
        scope = "AND merchant_id = $1::uuid" if merchant_id else ""
        params: list[Any] = []
        if merchant_id:
            params.append(str(merchant_id))
        params.extend([from_date, to_date, currency])
        p_from = f"${len(params) - 2}"
        p_to   = f"${len(params) - 1}"
        p_cur  = f"${len(params)}"

        async with get_connection() as c:
            row = await c.fetchrow(
                f"""
                SELECT
                    COUNT(*)                                                            AS total_count,
                    COUNT(*) FILTER (WHERE status = 'initiated')                        AS initiated_count,
                    COUNT(*) FILTER (WHERE status = 'processing')                       AS processing_count,
                    COUNT(*) FILTER (WHERE status = 'succeeded')                        AS succeeded_count,
                    COUNT(*) FILTER (WHERE status = 'failed')                           AS failed_count,
                    COUNT(*) FILTER (WHERE status = 'cancelled')                        AS cancelled_count,
                    COALESCE(SUM(amount), 0)                                            AS total_amount,
                    COALESCE(SUM(amount) FILTER (WHERE status = 'succeeded'), 0)        AS succeeded_amount,
                    COALESCE(SUM(amount) FILTER (WHERE kind = 'goodwill'), 0)           AS goodwill_amount
                FROM refunds
                WHERE currency = {p_cur}
                  AND created_at >= {p_from}::date
                  AND created_at <  ({p_to}::date + INTERVAL '1 day')
                  {scope}
                """,
                *params,
            )

        return {
            "merchant_id": str(merchant_id) if merchant_id else None,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "currency": currency,
            "total_count": row["total_count"],
            "initiated_count": row["initiated_count"],
            "processing_count": row["processing_count"],
            "succeeded_count": row["succeeded_count"],
            "failed_count": row["failed_count"],
            "cancelled_count": row["cancelled_count"],
            "total_amount": _d(row["total_amount"]),
            "succeeded_amount": _d(row["succeeded_amount"]),
            "goodwill_amount": _d(row["goodwill_amount"]),
        }

    # ────────────────────────────────────────────────────────────────
    # Dispute summary
    # ────────────────────────────────────────────────────────────────
    async def dispute_summary(
        self,
        *,
        merchant_id: str | UUID | None,
        from_date: date,
        to_date: date,
        currency: str = "INR",
    ) -> dict:
        scope = "AND merchant_id = $1::uuid" if merchant_id else ""
        params: list[Any] = []
        if merchant_id:
            params.append(str(merchant_id))
        params.extend([from_date, to_date, currency])
        p_from = f"${len(params) - 2}"
        p_to   = f"${len(params) - 1}"
        p_cur  = f"${len(params)}"

        async with get_connection() as c:
            row = await c.fetchrow(
                f"""
                SELECT
                    COUNT(*)                                                       AS total_count,
                    COUNT(*) FILTER (WHERE status = 'opened')                      AS opened_count,
                    COUNT(*) FILTER (WHERE status = 'under_review')                AS under_review_count,
                    COUNT(*) FILTER (WHERE status = 'evidence_submitted')          AS evidence_submitted_count,
                    COUNT(*) FILTER (WHERE status = 'won')                         AS won_count,
                    COUNT(*) FILTER (WHERE status = 'lost')                        AS lost_count,
                    COUNT(*) FILTER (WHERE status = 'withdrawn')                   AS withdrawn_count,
                    COALESCE(SUM(amount), 0)                                       AS total_amount,
                    COALESCE(SUM(amount) FILTER (WHERE status = 'lost'), 0)        AS lost_amount,
                    COALESCE(SUM(amount) FILTER (WHERE status = 'won'), 0)         AS won_amount
                FROM disputes
                WHERE currency = {p_cur}
                  AND opened_at >= {p_from}::date
                  AND opened_at <  ({p_to}::date + INTERVAL '1 day')
                  {scope}
                """,
                *params,
            )
        n = row["total_count"] or 0
        resolved = (row["won_count"] or 0) + (row["lost_count"] or 0) + (row["withdrawn_count"] or 0)
        win_rate = float(row["won_count"]) / resolved if resolved else 0.0

        return {
            "merchant_id": str(merchant_id) if merchant_id else None,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "currency": currency,
            "total_count": n,
            "opened_count": row["opened_count"],
            "under_review_count": row["under_review_count"],
            "evidence_submitted_count": row["evidence_submitted_count"],
            "won_count": row["won_count"],
            "lost_count": row["lost_count"],
            "withdrawn_count": row["withdrawn_count"],
            "total_amount": _d(row["total_amount"]),
            "lost_amount": _d(row["lost_amount"]),
            "won_amount": _d(row["won_amount"]),
            "win_rate": round(win_rate, 4),
        }

    # ────────────────────────────────────────────────────────────────
    # Daily rollups
    # ────────────────────────────────────────────────────────────────
    async def compute_daily_rollup(
        self,
        *,
        merchant_id: str | UUID,
        rollup_date: date,
        currency: str = "INR",
        computed_by: str | UUID | None = None,
    ) -> dict:
        async with get_connection() as c:
            j = await c.fetchval(
                "SELECT fn_compute_daily_rollup($1::uuid, $2::date, $3::char(3), $4::uuid)",
                str(merchant_id), rollup_date, currency,
                str(computed_by) if computed_by else None,
            )
        return json.loads(j) if isinstance(j, str) else j

    async def daily_rollups(
        self,
        *,
        merchant_id: str | UUID | None,
        from_date: date,
        to_date: date,
        currency: str = "INR",
    ) -> list[dict]:
        scope = "AND merchant_id = $1::uuid" if merchant_id else ""
        params: list[Any] = []
        if merchant_id:
            params.append(str(merchant_id))
        params.extend([from_date, to_date, currency])
        p_from = f"${len(params) - 2}"
        p_to   = f"${len(params) - 1}"
        p_cur  = f"${len(params)}"

        async with get_connection() as c:
            rows = await c.fetch(
                f"""
                SELECT * FROM merchant_daily_rollups
                WHERE currency = {p_cur}
                  AND rollup_date >= {p_from}::date
                  AND rollup_date <= {p_to}::date
                  {scope}
                ORDER BY rollup_date ASC, merchant_id ASC
                """,
                *params,
            )
        return [_row_to_rollup(r) for r in rows]

    async def monthly_rollups(
        self,
        *,
        merchant_id: str | UUID | None,
        from_date: date,
        to_date: date,
        currency: str = "INR",
    ) -> list[dict]:
        scope = "AND merchant_id = $1::uuid" if merchant_id else ""
        params: list[Any] = []
        if merchant_id:
            params.append(str(merchant_id))
        params.extend([from_date, to_date, currency])
        p_from = f"${len(params) - 2}"
        p_to   = f"${len(params) - 1}"
        p_cur  = f"${len(params)}"

        async with get_connection() as c:
            rows = await c.fetch(
                f"""
                SELECT
                    date_trunc('month', rollup_date)::date AS month_start,
                    SUM(orders_count)                       AS orders_count,
                    SUM(gross_sales)                        AS gross_sales,
                    SUM(payments_amount)                    AS payments_amount,
                    SUM(refunds_succeeded_amount)           AS refunds_succeeded_amount,
                    SUM(disputes_lost_amount)               AS chargebacks_amount,
                    SUM(fees_total)                         AS fees_total,
                    SUM(gst_total)                          AS gst_total,
                    SUM(ledger_net)                         AS ledger_net
                FROM merchant_daily_rollups
                WHERE currency = {p_cur}
                  AND rollup_date >= {p_from}::date
                  AND rollup_date <= {p_to}::date
                  {scope}
                GROUP BY date_trunc('month', rollup_date)
                ORDER BY month_start ASC
                """,
                *params,
            )
        out: list[dict] = []
        for r in rows:
            out.append({
                "month_start": r["month_start"].isoformat(),
                "currency": currency,
                "orders_count": int(r["orders_count"] or 0),
                "gross_sales": _d(r["gross_sales"]),
                "payments_amount": _d(r["payments_amount"]),
                "refunds_succeeded_amount": _d(r["refunds_succeeded_amount"]),
                "chargebacks_amount": _d(r["chargebacks_amount"]),
                "fees_total": _d(r["fees_total"]),
                "gst_total": _d(r["gst_total"]),
                "ledger_net": _d(r["ledger_net"]),
            })
        return out

    # ────────────────────────────────────────────────────────────────
    # CSV helpers
    # ────────────────────────────────────────────────────────────────
    def to_csv(self, rows: list[dict], *, filename: str) -> dict:
        buf = io.StringIO()
        if not rows:
            return {"filename": filename, "content_type": "text/csv", "body": ""}
        cols = list(rows[0].keys())
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows:
            w.writerow([_csv_cell(r.get(k)) for k in cols])
        return {"filename": filename, "content_type": "text/csv", "body": buf.getvalue()}

    def dict_to_csv(self, row: dict, *, filename: str) -> dict:
        return self.to_csv([row], filename=filename)


def _csv_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, Decimal):
        return f"{v:.4f}"
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return str(v)


reporting_service = ReportingService()
