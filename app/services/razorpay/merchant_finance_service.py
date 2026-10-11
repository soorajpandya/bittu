"""Merchant finance read model over local Razorpay mirrors and frozen allocations.

Merchant bank liability is the original online entitlement less proportional
processed refunds and confirmed bank payouts. Cash stays outside clearing.
Transfers explain funding progress; gateway fee breakdowns require actual fees.
KYC state controls payment eligibility, never hides an existing liability.
These projections do not post ERP journals or alter immutable audit ledgers.
"""

from __future__ import annotations

import csv
import io
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.core.database import get_connection, get_service_connection
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.services.razorpay.fee_policy import reporting_breakdown, rupees_to_paise
from app.services.razorpay.finance_queries import PAYMENTS_CTE, SETTLEMENTS_CTE

logger = get_logger(__name__)


_Q2 = Decimal("0.01")


# Statement entry categories (spec-mandated names).
ENTRY_PAYMENT_RECEIVED = "PAYMENT_RECEIVED"
ENTRY_COMMISSION_DEDUCTED = "COMMISSION_DEDUCTED"
ENTRY_TRANSFER_CREATED = "TRANSFER_CREATED"
ENTRY_SETTLEMENT_PROCESSED = "SETTLEMENT_PROCESSED"
ENTRY_REFUND = "REFUND"


# ─────────────────────────── helpers ──────────────────────────────────────


def _money(paise: Any) -> Decimal:
    """Paise (int) → rupees Decimal(2, ROUND_HALF_UP)."""
    try:
        return (Decimal(int(paise or 0)) / Decimal(100)).quantize(_Q2, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def _q(x: Decimal) -> Decimal:
    return x.quantize(_Q2, rounding=ROUND_HALF_UP)


async def _allocation_totals(conn, merchant_id, from_date=None, to_date=None):
    """Historical transfers or frozen entitlements, never today's rate × lifetime sales.

    Refunds are charged in the original merchant proportion. Missing historical
    allocations are explicitly counted for reconciliation instead of fabricated.
    """
    return await conn.fetchrow(
        """
        WITH allocated AS (
          SELECT p.razorpay_payment_id, p.amount_paise,
                 COALESCE(a.merchant_paise,t.amount_paise) AS merchant_paise,
                 COALESCE(r.refunded,0) AS refunded
          FROM rzp_payments p
          LEFT JOIN rzp_route_allocations a ON a.razorpay_payment_id=p.razorpay_payment_id AND a.merchant_id=p.merchant_id
          LEFT JOIN LATERAL (
            SELECT SUM(amount_paise)::bigint AS amount_paise FROM rzp_route_transfers t
            WHERE t.razorpay_payment_id=p.razorpay_payment_id AND t.merchant_id=p.merchant_id
              AND t.status::text <> 'failed'
          ) t ON true
          LEFT JOIN LATERAL (
            SELECT SUM(amount_paise)::bigint AS refunded FROM rzp_refunds r
            WHERE r.razorpay_payment_id=p.razorpay_payment_id AND r.merchant_id=p.merchant_id AND r.status='processed'
          ) r ON true
          WHERE p.merchant_id=$1::uuid AND p.captured=true
            AND ($2::date IS NULL OR (COALESCE(p.captured_at,p.created_at) AT TIME ZONE 'Asia/Kolkata')::date >= $2)
            AND ($3::date IS NULL OR (COALESCE(p.captured_at,p.created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3)
        )
        SELECT COALESCE(SUM(merchant_paise),0)::bigint AS merchant_paise,
               COALESCE(SUM(amount_paise-merchant_paise),0)::bigint AS deduction_paise,
               COALESCE(SUM(FLOOR(LEAST(refunded,amount_paise)::numeric * merchant_paise / NULLIF(amount_paise,0))),0)::bigint AS refund_share_paise,
               COUNT(*) FILTER (WHERE merchant_paise IS NULL)::int AS unresolved_payments
        FROM allocated
        """,
        merchant_id,
        from_date,
        to_date,
    )


async def _refund_allocations(conn, merchant_id, from_date=None, to_date=None):
    return await conn.fetch(
        """
        WITH refunds AS (
          SELECT r.*, p.amount_paise AS gross,
                 COALESCE(a.merchant_paise,t.merchant_paise) AS merchant_paise,
                 SUM(r.amount_paise) OVER (PARTITION BY r.razorpay_payment_id
                     ORDER BY COALESCE(r.processed_at,r.created_at),r.refund_id) AS cumulative
          FROM rzp_refunds r
          JOIN rzp_payments p ON p.razorpay_payment_id=r.razorpay_payment_id AND p.merchant_id=r.merchant_id
          LEFT JOIN rzp_route_allocations a ON a.razorpay_payment_id=r.razorpay_payment_id AND a.merchant_id=r.merchant_id
          LEFT JOIN LATERAL (
            SELECT SUM(amount_paise) AS merchant_paise FROM rzp_route_transfers t
            WHERE t.razorpay_payment_id=r.razorpay_payment_id AND t.merchant_id=r.merchant_id AND t.status::text<>'failed'
          ) t ON true
          WHERE r.merchant_id=$1::uuid AND r.status='processed' AND p.captured=true
        )
        SELECT *, (FLOOR(LEAST(cumulative,gross)*merchant_paise/NULLIF(gross,0))
                 - FLOOR(LEAST(cumulative-amount_paise,gross)*merchant_paise/NULLIF(gross,0)))::bigint AS merchant_refund_share
        FROM refunds
        WHERE ($2::date IS NULL OR (COALESCE(processed_at,created_at) AT TIME ZONE 'Asia/Kolkata')::date >= $2)
          AND ($3::date IS NULL OR (COALESCE(processed_at,created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3)
        """,
        merchant_id,
        from_date,
        to_date,
    )


@asynccontextmanager
async def _finance_connection(conn=None):
    if conn is not None:
        yield conn
    else:
        async with get_connection() as acquired:
            yield acquired


def _validate_window(first, last):
    if first is not None and last is not None and first > last:
        raise ValidationError("from_date must be <= to_date")


def _validate_page(limit, offset):
    if not 1 <= limit <= 10000 or offset < 0:
        raise ValidationError("limit must be 1..10000 and offset must be non-negative")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _require_merchant(merchant_id: Optional[str]) -> str:
    if not merchant_id:
        raise ValidationError("merchant context is required")
    return merchant_id


def _as_dict(value: Any) -> dict:
    """Coerce a JSONB column value (which asyncpg returns as raw str on
    this pool) into a dict so downstream ``.get(...)`` calls never blow up.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except Exception:
            return {}
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _wallet_status_for(
    account_status: Optional[str],
    product_status: Optional[str] = None,
) -> str:
    """Map (rzp_route_accounts.status, route_product_status) → wallet_status.

    Route linked accounts stay at ``status='created'`` for their entire
    happy-path lifetime — activation is signalled by the **product**
    flipping to ``activated``. So we must check product status first, and
    only fall back to the account-level status for terminal/negative
    states (suspended/rejected).
    """
    acc = (account_status or "").lower()
    prod = (product_status or "").lower()
    if acc == "suspended":
        return "suspended"
    if prod == "activated":
        return "active"
    if prod == "rejected" or acc == "rejected":
        return "kyc_rejected"
    if not account_status:
        return "pending_kyc"
    return "pending_kyc"


# ─────────────────────────── service ──────────────────────────────────────


class MerchantFinanceService:
    """Read-only finance projection over the local Razorpay mirror tables."""

    # ── activation gate ──────────────────────────────────────────────────

    async def _linked_account(self, merchant_id: str) -> Optional[dict]:
        """Fetch the merchant's linked-account snapshot (or None).

        Read via ``get_service_connection`` because ``rzp_route_accounts``
        is not RLS-scoped to ``app.tenant_id``.
        """
        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                "SELECT linked_account_id, status::text AS status, "
                "       kyc_status, activation_status, route_product_status "
                "FROM rzp_route_accounts "
                "WHERE merchant_id = $1::uuid",
                merchant_id,
            )
        return dict(row) if row else None

    async def fee_summary(self, merchant_id: str, *, from_date=None, to_date=None, branch_id=None) -> dict:
        merchant_id = _require_merchant(merchant_id)
        _validate_window(from_date, to_date)
        async with get_connection() as conn:
            rows = await conn.fetch(
                PAYMENTS_CTE + "SELECT * FROM payment_rows", merchant_id, from_date, to_date, branch_id
            )
        totals = {
            key: Decimal("0") for key in ("total_deduction", "razorpay_cut", "bittu_fee", "gst_on_fee", "platform_loss")
        }
        missing_fee = missing_allocation = 0
        for row in rows:
            if row["merchant_paise"] is None:
                missing_allocation += 1
                continue
            totals["total_deduction"] += _money(row["amount_paise"] - row["merchant_paise"])
            if row["fee_paise"] is None:
                missing_fee += 1
                continue
            split = reporting_breakdown(
                _money(row["amount_paise"]),
                method=row["method"],
                source=row["source"],
                merchant_paise=int(row["merchant_paise"]),
                actual_gateway_charges=_money(row["fee_paise"]),
            )
            for key in ("razorpay_cut", "bittu_fee", "gst_on_fee", "platform_loss"):
                totals[key] += split[key]
        complete = missing_fee == 0 and missing_allocation == 0
        return {
            **{
                key: float(value) if (complete or key == "total_deduction" and missing_allocation == 0) else None
                for key, value in totals.items()
            },
            "known_subtotals": {key: float(value) for key, value in totals.items()},
            "fee_breakdown_complete": complete,
            "missing_gateway_fee_count": missing_fee,
            "unresolved_payment_count": missing_allocation,
            "source": "razorpay_route_mirror",
            "basis": "captured_payments_before_refund_adjustments",
        }

    # ── wallet snapshot ──────────────────────────────────────────────────

    async def wallet_snapshot(self, merchant_id: str, *, from_date=None, to_date=None, branch_id=None) -> dict:
        """Activity in the requested window; balances are lifetime through its end."""
        merchant_id = _require_merchant(merchant_id)
        _validate_window(from_date, to_date)
        linked = await self._linked_account(merchant_id)
        async with get_connection() as conn:

            async def activity(first):
                return await conn.fetchrow(
                    PAYMENTS_CTE
                    + """
                    SELECT COALESCE(SUM(amount_paise),0)::bigint AS gross,
                           COALESCE(SUM(amount_paise) FILTER (WHERE source='cash'),0)::bigint AS cash,
                           COALESCE(SUM(amount_paise) FILTER (WHERE source='direct'),0)::bigint AS direct,
                           COALESCE(SUM(amount_paise) FILTER (WHERE source='razorpay'),0)::bigint AS online,
                           COALESCE(SUM(merchant_paise) FILTER (WHERE source='razorpay'),0)::bigint AS entitlement,
                           COALESCE(SUM(amount_paise-merchant_paise) FILTER (WHERE source='razorpay'),0)::bigint AS deduction,
                           COALESCE(SUM(transfer_paise),0)::bigint AS transferred,
                           COALESCE(SUM(refunded_paise),0)::bigint AS refunds,
                           COALESCE(SUM(refunded_paise) FILTER (WHERE source='cash'),0)::bigint AS cash_refunds,
                           COALESCE(SUM(refunded_paise) FILTER (WHERE source='direct'),0)::bigint AS direct_refunds,
                           COALESCE(SUM(FLOOR(LEAST(refunded_paise,amount_paise)::numeric * merchant_paise / NULLIF(amount_paise,0)))
                             FILTER (WHERE source='razorpay'),0)::bigint AS merchant_refunds,
                           COUNT(*)::bigint AS tx_count,
                           COUNT(*) FILTER (WHERE merchant_paise IS NULL)::bigint AS unresolved
                    FROM payment_rows
                """,
                    merchant_id,
                    first,
                    to_date,
                    branch_id,
                )

            window = await activity(from_date)
            lifetime = await activity(None) if from_date is not None else window

            async def bank(first):
                return await conn.fetchval(
                    SETTLEMENTS_CTE
                    + """
                    SELECT COALESCE(SUM(merchant_paise),0)::bigint FROM scoped_settlements
                    WHERE status='processed'
                """,
                    merchant_id,
                    first,
                    to_date,
                    branch_id,
                )

            settled_window = await bank(from_date)
            settled = await bank(None) if from_date is not None else settled_window
        pending = lifetime["entitlement"] - settled - lifetime["merchant_refunds"]
        available = lifetime["transferred"] - settled - lifetime["merchant_refunds"]
        return {
            "merchant_id": merchant_id,
            "wallet_status": _wallet_status_for(
                (linked or {}).get("status"), (linked or {}).get("route_product_status")
            ),
            "kyc_status": (linked or {}).get("kyc_status"),
            "linked_account_id": (linked or {}).get("linked_account_id"),
            "gross_sales": float(_money(window["gross"])),
            "cash_sales": float(_money(window["cash"])),
            "direct_sales": float(_money(window["direct"])),
            "online_sales": float(_money(window["online"])),
            "settled_amount": float(_money(
                settled_window + window["cash"] - window["cash_refunds"] + window["direct"] - window["direct_refunds"]
            )),
            "online_settled_amount": float(_money(settled_window)),
            "online_settled_lifetime": float(_money(settled)),
            "cash_balance": float(_money(lifetime["cash"] - lifetime["cash_refunds"])),
            "cash_refunds": float(_money(window["cash_refunds"])),
            "direct_balance": float(_money(lifetime["direct"] - lifetime["direct_refunds"])),
            "direct_refunds": float(_money(window["direct_refunds"])),
            "platform_commission": float(_money(window["deduction"])),
            "total_deduction": float(_money(window["deduction"])),
            "deduction_basis": "online_only",
            "merchant_refund_share": float(_money(lifetime["merchant_refunds"])),
            "unresolved_payment_count": int(lifetime["unresolved"]),
            "merchant_earned": float(_money(lifetime["entitlement"])),
            "transferred_to_linked": float(_money(lifetime["transferred"])),
            "pending_settlement": float(_money(max(pending, 0))),
            "balance_discrepancy": float(_money(min(pending, 0))),
            "refunds": float(_money(window["refunds"])),
            "available_balance": float(_money(max(available, 0))),
            "transaction_count": int(window["tx_count"]),
            "currency": "INR",
            "source": "razorpay_route_mirror",
            "balance_scope": "lifetime_through_to_date",
            "window": {
                "from": from_date.isoformat() if from_date else None,
                "to": to_date.isoformat() if to_date else None,
            },
        }

    # ── transactions list ────────────────────────────────────────────────

    @staticmethod
    def _project_payment(row: dict) -> dict:
        # Accept the previous query shape for compatibility with internal callers.
        pid = row.get("payment_id") or row.get("razorpay_payment_id")
        entitlement = row.get("merchant_paise", row.get("transfer_paise"))
        gross = _money(row["amount_paise"])
        source = row.get("source") or "razorpay"
        local = source in ("cash", "direct")
        notes = _as_dict(row.get("notes"))
        fee = row.get("fee_paise")
        split = (
            reporting_breakdown(
                gross,
                method=row.get("method"),
                source=source,
                actual_gateway_charges=_money(fee) if fee is not None else None,
                merchant_paise=int(entitlement),
            )
            if entitlement is not None
            else None
        )
        known_fee = split is not None and not split["gateway_charges_estimated"]
        deduction = gross - _money(entitlement) if entitlement is not None else None
        return {
            "transaction_id": pid,
            "payment_id": pid,
            "transfer_id": row.get("transfer_id"),
            "order_id": row.get("razorpay_order_id"),
            "internal_order_id": str(row["internal_order_id"]) if row.get("internal_order_id") else None,
            "amount": float(gross),
            "gross_amount": float(gross),
            "merchant_amount": float(_money(entitlement)) if entitlement is not None else None,
            "commission_amount": float(deduction) if deduction is not None else None,
            "total_deduction": float(deduction) if deduction is not None else None,
            "platform_commission": float(deduction) if deduction is not None else None,
            "bittu_fee": float(split["bittu_fee"]) if known_fee else None,
            "gst_on_fee": float(split["gst_on_fee"]) if known_fee else None,
            "platform_loss": float(split["platform_loss"]) if known_fee else None,
            "fee_basis": split["fee_basis"] if split is not None else None,
            "fee_breakdown_complete": known_fee,
            "allocation_status": "resolved" if entitlement is not None else "unresolved",
            "currency": row.get("currency") or "INR",
            "status": row.get("settlement_state") or row.get("status") or row.get("payment_status"),
            "settlement_status": row.get("settlement_state") or "pending",
            "settlement_id": row.get("settlement_id"),
            "payment_method": row.get("method"),
            "method_detail": {
                "vpa": row.get("upi_vpa"),
                "bank": row.get("bank_reference"),
                "wallet": None,
                "card_id": None,
            },
            "customer_name": notes.get("customer_name"),
            "customer_email": notes.get("customer_email"),
            "customer_contact": notes.get("customer_contact"),
            "captured": True,
            "captured_at": _iso(row.get("paid_at") or row.get("captured_at")),
            "created_at": _iso(row.get("paid_at") or row.get("created_at")),
            "fee": float(_money(fee)) if fee is not None else None,
            "tax": float(_money(row.get("tax_paise"))) if row.get("tax_paise") is not None else None,
            "refund_amount": float(_money(row.get("refunded_paise"))),
            "notes": notes,
            "source": source,
            "payment_kind": source,
            "channel": "online" if source == "razorpay" else source,
        }

    async def list_transactions(
        self,
        merchant_id: str,
        *,
        from_date=None,
        to_date=None,
        payment_method=None,
        min_amount=None,
        max_amount=None,
        search=None,
        limit=50,
        offset=0,
        branch_id=None,
        settlement_status=None,
        sort_by="created_at",
        sort_dir="desc",
    ) -> dict:
        merchant_id = _require_merchant(merchant_id)
        _validate_window(from_date, to_date)
        _validate_page(limit, offset)
        try:
            low = rupees_to_paise(min_amount) if min_amount is not None else None
            high = rupees_to_paise(max_amount) if max_amount is not None else None
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if low is not None and high is not None and low > high:
            raise ValidationError("min_amount must be <= max_amount")
        state = {"processed": "settled", "sent_to_bank": "processing"}.get(settlement_status, settlement_status)
        where = """
            WHERE ($5::text IS NULL OR LOWER(method)=$5 OR ($5='online' AND source='razorpay')
                   OR ($5 IN ('cash','direct','razorpay') AND source=$5))
              AND ($6::bigint IS NULL OR amount_paise >= $6)
              AND ($7::bigint IS NULL OR amount_paise <= $7)
              AND ($8::text IS NULL OR payment_id ILIKE $8 OR razorpay_order_id ILIKE $8
                   OR internal_order_id::text ILIKE $8 OR notes->>'customer_name' ILIKE $8
                   OR notes->>'customer_email' ILIKE $8 OR notes->>'customer_contact' ILIKE $8)
              AND ($9::text IS NULL OR settlement_state=$9 OR ($9='unsettled' AND source='razorpay' AND settlement_state NOT IN ('settled','refunded')))
        """
        params = [
            merchant_id,
            from_date,
            to_date,
            branch_id,
            payment_method.strip().lower() if payment_method else None,
            low,
            high,
            f"%{search.strip()}%" if search and search.strip() else None,
            state,
        ]
        sort = {
            "created_at": "paid_at",
            "gross_amount": "amount_paise",
            "amount": "amount_paise",
            "net_amount": "merchant_paise",
            "payment_method": "method",
        }.get(sort_by, "paid_at")
        direction = "ASC" if sort_dir.lower() == "asc" else "DESC"
        async with get_connection() as conn:
            total = await conn.fetchval(PAYMENTS_CTE + "SELECT COUNT(*) FROM payment_rows " + where, *params)
            rows = await conn.fetch(
                PAYMENTS_CTE
                + "SELECT * FROM payment_rows "
                + where
                + f" ORDER BY {sort} {direction} NULLS LAST, payment_id {direction} LIMIT $10 OFFSET $11",
                *params,
                limit,
                offset,
            )
        return {
            "items": [self._project_payment(dict(r)) for r in rows],
            "total": int(total or 0),
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < int(total or 0),
            "source": "razorpay_route_mirror",
        }

    # ── settlements list / detail / timeline ─────────────────────────────

    async def list_settlements(
        self,
        merchant_id: str,
        *,
        status=None,
        from_date=None,
        to_date=None,
        limit=50,
        offset=0,
        branch_id=None,
        sort_by="created_at",
        sort_dir="desc",
    ) -> dict:
        merchant_id = _require_merchant(merchant_id)
        _validate_window(from_date, to_date)
        _validate_page(limit, offset)
        state = {"settled": "processed", "sent_to_bank": "processing"}.get(status, status)
        params = [merchant_id, from_date, to_date, branch_id, state]
        where = "WHERE ($5::text IS NULL OR status=$5)"
        sort = {
            "created_at": "created_at",
            "gross_amount": "gross_paise",
            "net_settlement_amount": "merchant_paise",
        }.get(sort_by, "created_at")
        direction = "ASC" if sort_dir.lower() == "asc" else "DESC"
        async with get_connection() as conn:
            total = await conn.fetchval(SETTLEMENTS_CTE + "SELECT COUNT(*) FROM scoped_settlements " + where, *params)
            rows = await conn.fetch(
                SETTLEMENTS_CTE
                + "SELECT * FROM scoped_settlements "
                + where
                + f" ORDER BY {sort} {direction} NULLS LAST, settlement_id {direction} LIMIT $6 OFFSET $7",
                *params,
                limit,
                offset,
            )
        return {
            "items": [self._project_settlement_row(dict(r), merchant_id) for r in rows],
            "total": int(total or 0),
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < int(total or 0),
            "source": "razorpay_route_mirror",
        }

    @staticmethod
    def _project_settlement_row(row: dict, merchant_id: str) -> dict:
        # Zero or negative net recon amounts are legitimate; never substitute gross.
        return {
            "settlement_id": row["settlement_id"],
            "merchant_id": merchant_id,
            "gross_amount": float(_money(row.get("gross_paise"))),
            "merchant_amount": float(_money(row.get("merchant_paise"))),
            "commission_amount": None,
            "total_deduction": None,
            "refund_amount": float(_money(row.get("refund_paise"))),
            "fees": float(_money(row.get("fee_paise"))),
            "tax": float(_money(row.get("tax_paise"))),
            "transaction_count": int(row.get("tx_count") or 0),
            "reconciliation_complete": bool(row.get("line_count"))
            and row.get("header_amount_paise") is not None
            and row.get("reconciliation_net_paise") == row["header_amount_paise"],
            "reconciliation_net_amount": float(_money(row.get("reconciliation_net_paise"))),
            "utr": row.get("utr"),
            "status": row.get("status") or "pending",
            "settled_at": _iso(row.get("settled_at")),
            "created_at": _iso(row.get("created_at") or row.get("first_seen_at")),
            "currency": "INR",
            "source": "razorpay_route_mirror",
        }

    async def get_settlement(self, merchant_id: str, settlement_id: str, *, branch_id=None) -> dict:
        merchant_id = _require_merchant(merchant_id)
        async with get_connection() as conn:
            header = await conn.fetchrow(
                SETTLEMENTS_CTE + "SELECT * FROM scoped_settlements WHERE settlement_id=$5",
                merchant_id,
                None,
                None,
                branch_id,
                settlement_id,
            )
            if not header:
                raise NotFoundError("settlement", settlement_id)
            lines = await conn.fetch(
                """
                SELECT sp.* FROM rzp_settlement_payments sp
                WHERE sp.merchant_id=$1::uuid AND sp.settlement_id=$2
                  AND ($3::uuid IS NULL OR EXISTS (
                    SELECT 1 FROM rzp_payments p WHERE p.razorpay_payment_id=sp.razorpay_payment_id
                      AND p.merchant_id=$1::uuid AND p.branch_id=$3::uuid))
                ORDER BY sp.created_at, sp.id
            """,
                merchant_id,
                settlement_id,
                branch_id,
            )
        out = self._project_settlement_row(dict(header), merchant_id)
        out["payments"] = [
            {
                "payment_id": r["razorpay_payment_id"],
                "type": r["type"],
                "amount": float(_money(r["amount_paise"])),
                "fee": float(_money(r["fee_paise"])),
                "tax": float(_money(r["tax_paise"])),
                "credit": float(_money(r["credit_paise"])),
                "debit": float(_money(r["debit_paise"])),
                "created_at": _iso(r["created_at"]),
            }
            for r in lines
        ]
        return out

    async def settlement_timeline(self, merchant_id: str, settlement_id: str, *, branch_id=None) -> dict:
        # A mutable mirror cannot supply timestamps for unobserved prior states.
        settlement = await self.get_settlement(merchant_id, settlement_id, branch_id=branch_id)
        status = settlement["status"]
        return {
            "settlement_id": settlement_id,
            "current_status": status,
            "utr": settlement["utr"],
            "events": [
                {
                    "status": status,
                    "label": status.replace("_", " ").title(),
                    "completed": status == "processed",
                    "pending": status in ("pending", "created", "processing"),
                    "at": settlement["settled_at"] if status == "processed" else None,
                }
            ],
            "history_complete": False,
            "source": "razorpay_route_mirror",
        }

    # ── CSV export ───────────────────────────────────────────────────────

    async def export_csv(self, merchant_id: str, *, from_date=None, to_date=None) -> tuple[str, str]:
        merchant_id = _require_merchant(merchant_id)
        end = to_date or datetime.now(ZoneInfo("Asia/Kolkata")).date()
        start = from_date or end - timedelta(days=30)
        _validate_window(start, end)
        async with get_connection() as conn:
            rows = await conn.fetch(
                PAYMENTS_CTE + "SELECT * FROM payment_rows ORDER BY paid_at, payment_id", merchant_id, start, end, None
            )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "Date",
                "Payment ID",
                "Transfer ID",
                "Settlement ID",
                "Gross Amount",
                "Total Deduction",
                "Merchant Amount",
                "Refund",
                "Status",
                "Method",
                "Allocation Status",
            ]
        )

        def formatted(value):
            return "" if value is None else f"{value:.2f}"

        for row in rows:
            r = self._project_payment(dict(row))
            writer.writerow(
                [
                    r["created_at"],
                    r["payment_id"],
                    r["transfer_id"],
                    r["settlement_id"],
                    formatted(r["gross_amount"]),
                    formatted(r["total_deduction"]),
                    formatted(r["merchant_amount"]),
                    formatted(r["refund_amount"]),
                    r["status"],
                    r["payment_method"],
                    r["allocation_status"],
                ]
            )
        return buf.getvalue(), f"statement_{merchant_id}_{start}_{end}.csv"

    # ── ledger balance ───────────────────────────────────────────────────

    async def ledger_balance(self, merchant_id: str) -> dict:
        wallet = await self.wallet_snapshot(merchant_id)
        return {
            **wallet,
            "current_balance": wallet["pending_settlement"],
            "gross_sales": wallet["online_sales"],
            "settled_amount": wallet["online_settled_lifetime"],
            "refunded_amount": wallet["merchant_refund_share"],
        }

    # ── ledger entries (statement projection) ────────────────────────────

    async def _build_entries(self, merchant_id: str, *, from_date=None, to_date=None, conn=None) -> list[dict]:
        """Merchant entitlement less processed refunds and confirmed bank payouts.

        Transfers explain clearing progress without crediting the entitlement twice.
        Compute the opening balance from all prior events before filtering the window.
        """
        _validate_window(from_date, to_date)
        async with _finance_connection(conn) as conn:
            payments = await conn.fetch(
                PAYMENTS_CTE + "SELECT * FROM payment_rows WHERE source='razorpay'", merchant_id, None, to_date, None
            )
            settlements = await conn.fetch(
                SETTLEMENTS_CTE + "SELECT * FROM scoped_settlements WHERE status='processed'",
                merchant_id,
                None,
                to_date,
                None,
            )
            transfers = await conn.fetch(
                """
                SELECT * FROM rzp_route_transfers WHERE merchant_id=$1::uuid AND status::text<>'failed'
                  AND ($2::date IS NULL OR (COALESCE(processed_at,created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $2)
            """,
                merchant_id,
                to_date,
            )
            refunds = await _refund_allocations(conn, merchant_id, None, to_date)
        events = []

        def add(
            at, priority, entry_id, kind, direction, source, paise, reference, *, affects=True, currency="INR", **extra
        ):
            if at is None:
                return
            events.append(
                (
                    at,
                    priority,
                    entry_id,
                    {
                        "entry_id": entry_id,
                        "entry_type": kind,
                        "type": direction,
                        "source": source,
                        "amount": float(_money(paise)),
                        "reference": reference,
                        "at": _iso(at),
                        "description": kind.replace("_", " ").title(),
                        "currency": currency,
                        "affects_balance": affects,
                        **extra,
                    },
                )
            )

        for p in payments:
            pid = p["payment_id"]
            resolved = p["merchant_paise"] is not None
            add(
                p["paid_at"],
                0,
                "pay:" + pid,
                ENTRY_PAYMENT_RECEIVED,
                "CREDIT",
                "payment",
                p["amount_paise"],
                pid,
                affects=resolved,
                order_id=str(p["internal_order_id"]) if p["internal_order_id"] else None,
                allocation_status="resolved" if resolved else "unresolved",
            )
            if resolved:
                add(
                    p["paid_at"],
                    1,
                    "com:" + pid,
                    ENTRY_COMMISSION_DEDUCTED,
                    "DEBIT",
                    "commission",
                    p["amount_paise"] - p["merchant_paise"],
                    pid,
                    total_deduction=True,
                )
        for t in transfers:
            add(
                t["processed_at"] or t["created_at"],
                2,
                "trf:" + t["transfer_id"],
                ENTRY_TRANSFER_CREATED,
                "CREDIT",
                "transfer",
                t["amount_paise"],
                t["transfer_id"],
                affects=False,
                payment_id=t["razorpay_payment_id"],
            )
        for r in refunds:
            resolved = r["merchant_refund_share"] is not None
            add(
                r["processed_at"] or r["created_at"],
                3,
                "ref:" + r["refund_id"],
                ENTRY_REFUND,
                "DEBIT",
                "refund",
                r["merchant_refund_share"] if resolved else r["amount_paise"],
                r["refund_id"],
                affects=resolved,
                allocation_status="resolved" if resolved else "unresolved",
                payment_id=r["razorpay_payment_id"],
            )
        for r in settlements:
            add(
                r["settled_at"] or r["created_at"],
                4,
                "setl:" + r["settlement_id"],
                ENTRY_SETTLEMENT_PROCESSED,
                "DEBIT",
                "settlement",
                r["merchant_paise"],
                r["settlement_id"],
                utr=r["utr"],
            )
        running = Decimal("0.00")
        out = []
        for at, _, _, entry in sorted(events, key=lambda event: event[:3]):
            if entry["affects_balance"]:
                running += Decimal(str(entry["amount"])) * (1 if entry["type"] == "CREDIT" else -1)
            entry["balance_after"] = float(_q(running))
            if from_date is None or at.astimezone(ZoneInfo("Asia/Kolkata")).date() >= from_date:
                out.append(entry)
        return out

    async def list_ledger_entries(
        self, merchant_id: str, *, from_date=None, to_date=None, entry_type=None, source=None, limit=50, offset=0
    ) -> dict:
        merchant_id = _require_merchant(merchant_id)
        _validate_page(limit, offset)
        stream = list(reversed(await self._build_entries(merchant_id, from_date=from_date, to_date=to_date)))
        if entry_type:
            stream = [e for e in stream if e["type"] == entry_type.upper() or e["entry_type"] == entry_type.upper()]
        if source:
            stream = [e for e in stream if e["source"] == source.lower()]
        return {
            "items": stream[offset : offset + limit],
            "total": len(stream),
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < len(stream),
            "source": "razorpay_route_mirror",
        }

    async def get_ledger_entry(self, merchant_id: str, entry_id: str) -> dict:
        merchant_id = _require_merchant(merchant_id)
        if ":" not in entry_id:
            raise ValidationError(
                "entry_id must be of the form <prefix>:<razorpay_id> "
                "where prefix is one of pay / com / trf / setl / ref"
            )
        stream = await self._build_entries(merchant_id, from_date=None, to_date=None)
        for e in stream:
            if e["entry_id"] == entry_id:
                return e
        raise NotFoundError(f"Ledger entry {entry_id} not found.")

    async def ledger_consistency_check(self, merchant_id: str) -> dict:
        """Recompute the projection from the local mirror and verify invariants."""
        merchant_id = _require_merchant(merchant_id)
        balance = await self.ledger_balance(merchant_id)
        entries = await self._build_entries(merchant_id, from_date=None, to_date=None)

        total_credit = sum(
            (Decimal(str(e["amount"])) for e in entries if e["type"] == "CREDIT" and e.get("affects_balance")),
            Decimal("0.00"),
        )
        total_debit = sum(
            (Decimal(str(e["amount"])) for e in entries if e["type"] == "DEBIT" and e.get("affects_balance")),
            Decimal("0.00"),
        )
        derived = total_credit - total_debit
        derived = _q(derived)
        expected = Decimal(str(balance["pending_settlement"])) + Decimal(str(balance["balance_discrepancy"]))
        delta = _q(derived - expected)
        return {
            "merchant_id": merchant_id,
            "consistent": delta == Decimal("0.00") and balance["unresolved_payment_count"] == 0,
            "derived_balance": float(derived),
            "live_balance": float(expected),
            "delta": float(delta),
            "total_credit": float(_q(total_credit)),
            "total_debit": float(_q(total_debit)),
            "entry_count": len(entries),
            "source": "razorpay_route_mirror",
        }


merchant_finance_service = MerchantFinanceService()
