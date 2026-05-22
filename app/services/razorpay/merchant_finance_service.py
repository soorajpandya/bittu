"""
Razorpay-backed merchant finance projection layer.

Source of truth: the **local Razorpay mirror tables** (`rzp_payments`,
`rzp_refunds`, `rzp_settlements`, `rzp_settlement_payments`). These are
kept in sync with Razorpay's master account by `webhook_dispatcher.py`
and the polling/recon services. Every row is tagged with a `merchant_id`
that is resolved from `notes.merchant_id` at ingest time.

Why local, not live Razorpay
----------------------------
Razorpay's `/v1/payments` is **master-account-wide** — all merchants
share the same listing — so filtering would require paginating every
payment on the platform and matching `notes.merchant_id` client-side.
The local mirror is already partitioned by `merchant_id` (indexed),
so a per-merchant query is a single indexed scan instead of an O(N)
walk over the whole platform.

Commission model
----------------
Bittu retains a flat **5%** of every captured payment:

    merchant_amount    = gross × 0.95
    commission_amount  = gross × 0.05

The split is computed at projection time; it is not stored anywhere
in the database.

Tenant isolation
----------------
Every query hard-filters by `merchant_id = $1::uuid` where `merchant_id`
is the caller's `UserContext.restaurant_id`. There is no Route linked
account requirement — any merchant with an `rzp_payments` row can read
their finance views.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from app.core.database import get_connection
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)


COMMISSION_RATE = Decimal("0.05")
MERCHANT_RATE = Decimal("0.95")
_Q2 = Decimal("0.01")


# ─────────────────────────── helpers ──────────────────────────────────────


def _money(paise: Any) -> Decimal:
    """Paise (int) → rupees Decimal(2, ROUND_HALF_UP)."""
    try:
        return (Decimal(int(paise or 0)) / Decimal(100)).quantize(_Q2, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def _q(x: Decimal) -> Decimal:
    return x.quantize(_Q2, rounding=ROUND_HALF_UP)


def _split_gross(gross_rupees: Decimal) -> tuple[Decimal, Decimal]:
    """Return (merchant_amount, commission_amount) summing to gross."""
    if gross_rupees <= 0:
        return Decimal("0.00"), Decimal("0.00")
    merchant = _q(gross_rupees * MERCHANT_RATE)
    commission = _q(gross_rupees - merchant)
    return merchant, commission


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


# ─────────────────────────── service ──────────────────────────────────────


class MerchantFinanceService:
    """Read-only finance projection over the local Razorpay mirror tables."""

    # ── wallet snapshot ──────────────────────────────────────────────────

    async def wallet_snapshot(
        self,
        merchant_id: str,
        *,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
    ) -> dict:
        merchant_id = _require_merchant(merchant_id)
        async with get_connection() as conn:
            pay = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(amount_paise), 0)::bigint AS gross_paise,
                       COUNT(*)::bigint                       AS tx_count
                FROM rzp_payments
                WHERE merchant_id = $1::uuid
                  AND status      = 'captured'
                  AND ($2::date IS NULL OR (COALESCE(captured_at, created_at))::date >= $2::date)
                  AND ($3::date IS NULL OR (COALESCE(captured_at, created_at))::date <= $3::date)
                """,
                merchant_id, from_date, to_date,
            )
            setl_gross = await conn.fetchval(
                """
                SELECT COALESCE(SUM(amount_paise), 0)::bigint
                FROM rzp_settlement_payments
                WHERE merchant_id = $1::uuid
                  AND type        = 'payment'
                  AND ($2::date IS NULL OR created_at::date >= $2::date)
                  AND ($3::date IS NULL OR created_at::date <= $3::date)
                """,
                merchant_id, from_date, to_date,
            )
            refunded = await conn.fetchval(
                """
                SELECT COALESCE(SUM(amount_paise), 0)::bigint
                FROM rzp_refunds
                WHERE merchant_id = $1::uuid
                  AND status      IN ('pending', 'processed')
                  AND ($2::date IS NULL OR created_at::date >= $2::date)
                  AND ($3::date IS NULL OR created_at::date <= $3::date)
                """,
                merchant_id, from_date, to_date,
            )

        gross = _money(pay["gross_paise"])
        merchant_share, commission = _split_gross(gross)
        # Settled-share of gross = settled gross × 95%
        settled_gross = _money(setl_gross or 0)
        settled_share, _ = _split_gross(settled_gross)
        refunds = _money(refunded or 0)

        pending = merchant_share - settled_share
        if pending < 0:
            pending = Decimal("0.00")
        available = settled_share - refunds
        if available < 0:
            available = Decimal("0.00")

        return {
            "merchant_id": merchant_id,
            "gross_sales": float(gross),
            "settled_amount": float(_q(settled_share)),
            "platform_commission": float(commission),
            "pending_settlement": float(_q(pending)),
            "refunds": float(refunds),
            "available_balance": float(_q(available)),
            "transaction_count": int(pay["tx_count"]),
            "currency": "INR",
            "window": {
                "from": from_date.isoformat() if from_date else None,
                "to": to_date.isoformat() if to_date else None,
            },
        }

    # ── transactions list ────────────────────────────────────────────────

    @staticmethod
    def _project_payment(row: dict) -> dict:
        gross = _money(row["amount_paise"])
        merchant, commission = _split_gross(gross)
        notes = row.get("notes") or {}
        return {
            "transaction_id": row["razorpay_payment_id"],
            "payment_id": row["razorpay_payment_id"],
            "order_id": row.get("razorpay_order_id"),
            "internal_order_id": (
                str(row["internal_order_id"]) if row.get("internal_order_id") else None
            ),
            "amount": float(gross),
            "gross_amount": float(gross),
            "merchant_amount": float(merchant),
            "commission_amount": float(commission),
            "currency": row.get("currency") or "INR",
            "status": row["status"],
            "payment_method": row.get("method"),
            "method_detail": {
                "vpa": row.get("upi_vpa"),
                "bank": row.get("bank_reference"),
                "wallet": None,
                "card_id": None,
            },
            "customer_email": notes.get("customer_email"),
            "customer_contact": notes.get("customer_contact"),
            "customer_name": notes.get("customer_name"),
            "captured": bool(row.get("captured")),
            "captured_at": _iso(row.get("captured_at")),
            "created_at": _iso(row.get("created_at")),
            "fee": float(_money(row.get("fee_paise"))),
            "tax": float(_money(row.get("tax_paise"))),
            "description": notes.get("description"),
            "notes": notes,
            "error_code": row.get("error_code"),
            "error_description": row.get("error_description"),
            "source": "razorpay",
        }

    async def list_transactions(
        self,
        merchant_id: str,
        *,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        payment_method: Optional[str] = None,
        min_amount: Optional[float] = None,
        max_amount: Optional[float] = None,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        merchant_id = _require_merchant(merchant_id)
        min_paise = int(round(min_amount * 100)) if min_amount is not None else None
        max_paise = int(round(max_amount * 100)) if max_amount is not None else None
        like = f"%{search.strip()}%" if search and search.strip() else None

        async with get_connection() as conn:
            total = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM rzp_payments
                WHERE merchant_id = $1::uuid
                  AND status      = 'captured'
                  AND ($2::date  IS NULL OR (COALESCE(captured_at, created_at))::date >= $2::date)
                  AND ($3::date  IS NULL OR (COALESCE(captured_at, created_at))::date <= $3::date)
                  AND ($4::text  IS NULL OR method = $4::text)
                  AND ($5::bigint IS NULL OR amount_paise >= $5::bigint)
                  AND ($6::bigint IS NULL OR amount_paise <= $6::bigint)
                  AND ($7::text  IS NULL
                       OR razorpay_payment_id ILIKE $7
                       OR razorpay_order_id   ILIKE $7
                       OR upi_vpa             ILIKE $7
                       OR (notes ->> 'customer_email')   ILIKE $7
                       OR (notes ->> 'customer_contact') ILIKE $7
                       OR (notes ->> 'customer_name')    ILIKE $7)
                """,
                merchant_id, from_date, to_date,
                payment_method, min_paise, max_paise, like,
            )
            rows = await conn.fetch(
                """
                SELECT razorpay_payment_id, razorpay_order_id, internal_order_id,
                       amount_paise, fee_paise, tax_paise, currency,
                       method, upi_vpa, bank_reference,
                       status::text AS status, captured, captured_at,
                       error_code, error_description, notes, created_at
                FROM rzp_payments
                WHERE merchant_id = $1::uuid
                  AND status      = 'captured'
                  AND ($2::date  IS NULL OR (COALESCE(captured_at, created_at))::date >= $2::date)
                  AND ($3::date  IS NULL OR (COALESCE(captured_at, created_at))::date <= $3::date)
                  AND ($4::text  IS NULL OR method = $4::text)
                  AND ($5::bigint IS NULL OR amount_paise >= $5::bigint)
                  AND ($6::bigint IS NULL OR amount_paise <= $6::bigint)
                  AND ($7::text  IS NULL
                       OR razorpay_payment_id ILIKE $7
                       OR razorpay_order_id   ILIKE $7
                       OR upi_vpa             ILIKE $7
                       OR (notes ->> 'customer_email')   ILIKE $7
                       OR (notes ->> 'customer_contact') ILIKE $7
                       OR (notes ->> 'customer_name')    ILIKE $7)
                ORDER BY COALESCE(captured_at, created_at) DESC, razorpay_payment_id DESC
                LIMIT $8 OFFSET $9
                """,
                merchant_id, from_date, to_date,
                payment_method, min_paise, max_paise, like,
                limit, offset,
            )

        items = [self._project_payment(dict(r)) for r in rows]
        return {
            "items": items,
            "total": int(total or 0),
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < int(total or 0),
        }

    # ── settlements list / detail / timeline ─────────────────────────────

    async def list_settlements(
        self,
        merchant_id: str,
        *,
        status: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        merchant_id = _require_merchant(merchant_id)
        async with get_connection() as conn:
            total = await conn.fetchval(
                """
                SELECT COUNT(*) FROM (
                    SELECT sp.settlement_id
                    FROM rzp_settlement_payments sp
                    LEFT JOIN rzp_settlements s ON s.settlement_id = sp.settlement_id
                    WHERE sp.merchant_id = $1::uuid
                      AND ($2::text IS NULL OR s.status::text = $2::text)
                      AND ($3::date IS NULL OR COALESCE(s.settled_at::date, sp.created_at::date) >= $3::date)
                      AND ($4::date IS NULL OR COALESCE(s.settled_at::date, sp.created_at::date) <= $4::date)
                    GROUP BY sp.settlement_id
                ) g
                """,
                merchant_id, status, from_date, to_date,
            )
            rows = await conn.fetch(
                """
                SELECT sp.settlement_id,
                       COALESCE(SUM(CASE WHEN sp.type = 'payment'
                                         THEN sp.amount_paise ELSE 0 END), 0)::bigint AS gross_paise,
                       COALESCE(SUM(CASE WHEN sp.type = 'refund'
                                         THEN sp.amount_paise ELSE 0 END), 0)::bigint AS refund_paise,
                       COALESCE(SUM(sp.fee_paise), 0)::bigint  AS fee_paise,
                       COALESCE(SUM(sp.tax_paise), 0)::bigint  AS tax_paise,
                       COUNT(*) FILTER (WHERE sp.type = 'payment')::bigint AS tx_count,
                       MAX(s.utr)                              AS utr,
                       COALESCE(MAX(s.status::text), 'pending') AS status,
                       MAX(s.settled_at)                       AS settled_at,
                       MIN(sp.created_at)                      AS first_seen_at
                FROM rzp_settlement_payments sp
                LEFT JOIN rzp_settlements s ON s.settlement_id = sp.settlement_id
                WHERE sp.merchant_id = $1::uuid
                  AND ($2::text IS NULL OR s.status::text = $2::text)
                  AND ($3::date IS NULL OR COALESCE(s.settled_at::date, sp.created_at::date) >= $3::date)
                  AND ($4::date IS NULL OR COALESCE(s.settled_at::date, sp.created_at::date) <= $4::date)
                GROUP BY sp.settlement_id
                ORDER BY COALESCE(MAX(s.settled_at), MIN(sp.created_at)) DESC NULLS LAST
                LIMIT $5 OFFSET $6
                """,
                merchant_id, status, from_date, to_date, limit, offset,
            )

        items = [self._project_settlement_row(dict(r), merchant_id) for r in rows]
        return {
            "items": items,
            "total": int(total or 0),
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < int(total or 0),
        }

    @staticmethod
    def _project_settlement_row(row: dict, merchant_id: str) -> dict:
        gross = _money(row["gross_paise"])
        refunds = _money(row["refund_paise"])
        net_gross = gross - refunds
        if net_gross < 0:
            net_gross = Decimal("0.00")
        merchant, commission = _split_gross(net_gross)
        return {
            "settlement_id": row["settlement_id"],
            "merchant_id": merchant_id,
            "gross_amount": float(_q(net_gross)),
            "merchant_amount": float(merchant),
            "commission_amount": float(commission),
            "refund_amount": float(refunds),
            "fees": float(_money(row.get("fee_paise"))),
            "tax": float(_money(row.get("tax_paise"))),
            "transaction_count": int(row.get("tx_count") or 0),
            "utr": row.get("utr"),
            "status": row.get("status") or "pending",
            "settled_at": _iso(row.get("settled_at")),
            "created_at": _iso(row.get("settled_at") or row.get("first_seen_at")),
            "currency": "INR",
        }

    async def get_settlement(self, merchant_id: str, settlement_id: str) -> dict:
        merchant_id = _require_merchant(merchant_id)
        async with get_connection() as conn:
            header = await conn.fetchrow(
                """
                SELECT sp.settlement_id,
                       COALESCE(SUM(CASE WHEN sp.type = 'payment'
                                         THEN sp.amount_paise ELSE 0 END), 0)::bigint AS gross_paise,
                       COALESCE(SUM(CASE WHEN sp.type = 'refund'
                                         THEN sp.amount_paise ELSE 0 END), 0)::bigint AS refund_paise,
                       COALESCE(SUM(sp.fee_paise), 0)::bigint  AS fee_paise,
                       COALESCE(SUM(sp.tax_paise), 0)::bigint  AS tax_paise,
                       COUNT(*) FILTER (WHERE sp.type = 'payment')::bigint AS tx_count,
                       MAX(s.utr)                              AS utr,
                       COALESCE(MAX(s.status::text), 'pending') AS status,
                       MAX(s.settled_at)                       AS settled_at,
                       MIN(sp.created_at)                      AS first_seen_at
                FROM rzp_settlement_payments sp
                LEFT JOIN rzp_settlements s ON s.settlement_id = sp.settlement_id
                WHERE sp.merchant_id = $1::uuid
                  AND sp.settlement_id = $2
                GROUP BY sp.settlement_id
                """,
                merchant_id, settlement_id,
            )
            if not header:
                raise NotFoundError(f"Settlement {settlement_id} not found for this merchant.")

            lines = await conn.fetch(
                """
                SELECT sp.razorpay_payment_id, sp.type, sp.amount_paise,
                       sp.fee_paise, sp.tax_paise, sp.credit_paise, sp.debit_paise,
                       sp.created_at,
                       p.method, p.upi_vpa
                FROM rzp_settlement_payments sp
                LEFT JOIN rzp_payments_index pi
                       ON pi.razorpay_payment_id = sp.razorpay_payment_id
                LEFT JOIN rzp_payments p
                       ON p.id = pi.payment_uuid
                WHERE sp.merchant_id  = $1::uuid
                  AND sp.settlement_id = $2
                ORDER BY sp.created_at ASC
                """,
                merchant_id, settlement_id,
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
                "method": r.get("method"),
                "vpa": r.get("upi_vpa"),
                "created_at": _iso(r["created_at"]),
            }
            for r in lines
        ]
        return out

    async def settlement_timeline(self, merchant_id: str, settlement_id: str) -> dict:
        """Lifecycle derived from rzp_settlements.status + settled_at."""
        merchant_id = _require_merchant(merchant_id)
        async with get_connection() as conn:
            exists = await conn.fetchval(
                """
                SELECT 1 FROM rzp_settlement_payments
                WHERE merchant_id = $1::uuid AND settlement_id = $2
                LIMIT 1
                """,
                merchant_id, settlement_id,
            )
            if not exists:
                raise NotFoundError(f"Settlement {settlement_id} not found for this merchant.")
            meta = await conn.fetchrow(
                """
                SELECT status::text AS status, utr, settled_at, created_at
                FROM rzp_settlements
                WHERE settlement_id = $1
                LIMIT 1
                """,
                settlement_id,
            )

        status = (meta["status"] if meta else "pending").lower()
        anchor_dt = (meta and (meta["settled_at"] or meta["created_at"])) or None
        anchor = _iso(anchor_dt)

        ORDER = ["pending", "processing", "processed", "failed"]
        failed = status == "failed"
        events: list[dict] = []
        for st in ORDER:
            if failed:
                done = st in ("pending", "processing", "failed")
                pending_flag = st == "processed"
            else:
                done = (st in ORDER and ORDER.index(st) <= ORDER.index(status)) if status in ORDER else False
                pending_flag = not done
            events.append({
                "status": st,
                "label": st.replace("_", " ").title(),
                "completed": done,
                "pending": pending_flag,
                "at": anchor if done else None,
            })

        return {
            "settlement_id": settlement_id,
            "current_status": status,
            "utr": meta["utr"] if meta else None,
            "events": events,
            "source": "razorpay",
        }

    # ── CSV export ───────────────────────────────────────────────────────

    async def export_csv(
        self,
        merchant_id: str,
        *,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
    ) -> tuple[str, str]:
        merchant_id = _require_merchant(merchant_id)
        today = date.today()
        end = to_date or today
        start = from_date or (end - timedelta(days=30))
        if start > end:
            raise ValidationError("from_date must be <= to_date")

        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT p.razorpay_payment_id           AS payment_id,
                       COALESCE(sp.settlement_id, '') AS settlement_id,
                       p.amount_paise                  AS gross_paise,
                       COALESCE(r.refund_paise, 0)    AS refund_paise,
                       p.status::text                  AS status,
                       COALESCE(p.captured_at, p.created_at) AS at,
                       p.method                        AS method,
                       p.notes                         AS notes
                FROM rzp_payments p
                LEFT JOIN LATERAL (
                    SELECT settlement_id
                    FROM rzp_settlement_payments
                    WHERE razorpay_payment_id = p.razorpay_payment_id
                      AND merchant_id         = p.merchant_id
                      AND type = 'payment'
                    LIMIT 1
                ) sp ON TRUE
                LEFT JOIN LATERAL (
                    SELECT COALESCE(SUM(amount_paise), 0)::bigint AS refund_paise
                    FROM rzp_refunds
                    WHERE razorpay_payment_id = p.razorpay_payment_id
                      AND merchant_id         = p.merchant_id
                      AND status IN ('pending', 'processed')
                ) r ON TRUE
                WHERE p.merchant_id = $1::uuid
                  AND p.status      = 'captured'
                  AND (COALESCE(p.captured_at, p.created_at))::date BETWEEN $2::date AND $3::date
                ORDER BY COALESCE(p.captured_at, p.created_at) ASC
                """,
                merchant_id, start, end,
            )

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "Date", "Payment ID", "Settlement ID",
            "Gross Amount", "Merchant Amount", "Commission",
            "Refund", "Status", "Method",
        ])
        for r in rows:
            gross_rupees = _money(r["gross_paise"])
            merchant_rupees, commission_rupees = _split_gross(gross_rupees)
            refund_rupees = _money(r["refund_paise"])
            w.writerow([
                _iso(r["at"]) or "",
                r["payment_id"],
                r["settlement_id"] or "",
                f"{gross_rupees:.2f}",
                f"{merchant_rupees:.2f}",
                f"{commission_rupees:.2f}",
                f"{refund_rupees:.2f}",
                r["status"],
                r.get("method") or "",
            ])

        filename = f"statement_{merchant_id}_{start.isoformat()}_{end.isoformat()}.csv"
        return buf.getvalue(), filename

    # ── ledger balance ───────────────────────────────────────────────────

    async def ledger_balance(self, merchant_id: str) -> dict:
        merchant_id = _require_merchant(merchant_id)
        async with get_connection() as conn:
            captured = await conn.fetchval(
                """
                SELECT COALESCE(SUM(amount_paise), 0)::bigint
                FROM rzp_payments
                WHERE merchant_id = $1::uuid AND status = 'captured'
                """,
                merchant_id,
            )
            refunded = await conn.fetchval(
                """
                SELECT COALESCE(SUM(amount_paise), 0)::bigint
                FROM rzp_refunds
                WHERE merchant_id = $1::uuid AND status IN ('pending', 'processed')
                """,
                merchant_id,
            )
            settled_gross = await conn.fetchval(
                """
                SELECT COALESCE(SUM(amount_paise), 0)::bigint
                FROM rzp_settlement_payments
                WHERE merchant_id = $1::uuid AND type = 'payment'
                """,
                merchant_id,
            )

        gross = _money(captured or 0)
        merchant_share, _ = _split_gross(gross)
        refunds = _money(refunded or 0)
        settled_share, _ = _split_gross(_money(settled_gross or 0))

        current = merchant_share - refunds
        if current < 0:
            current = Decimal("0.00")
        pending = merchant_share - settled_share
        if pending < 0:
            pending = Decimal("0.00")

        return {
            "merchant_id": merchant_id,
            "current_balance": float(_q(current)),
            "pending_settlement": float(_q(pending)),
            "settled_amount": float(_q(settled_share)),
            "refunded_amount": float(refunds),
            "currency": "INR",
        }

    # ── ledger entries (synthetic projection) ────────────────────────────

    async def _build_entries(
        self,
        merchant_id: str,
        *,
        from_date: Optional[date],
        to_date: Optional[date],
    ) -> list[dict]:
        """
        Materialise the merchant's ledger as a chronological stream:
          CREDIT  payment       (per captured payment)
          DEBIT   commission    (5% of each captured payment, synthetic)
          DEBIT   settlement    (per settlement_id, merchant's share)
          DEBIT   refund        (per refund)
        Returns ascending by timestamp with `balance_after` filled in.
        """
        async with get_connection() as conn:
            payments = await conn.fetch(
                """
                SELECT razorpay_payment_id, razorpay_order_id, amount_paise,
                       currency, captured_at, created_at
                FROM rzp_payments
                WHERE merchant_id = $1::uuid
                  AND status      = 'captured'
                  AND ($2::date IS NULL OR (COALESCE(captured_at, created_at))::date >= $2::date)
                  AND ($3::date IS NULL OR (COALESCE(captured_at, created_at))::date <= $3::date)
                """,
                merchant_id, from_date, to_date,
            )
            settlements = await conn.fetch(
                """
                SELECT sp.settlement_id,
                       SUM(CASE WHEN sp.type = 'payment'
                                THEN sp.amount_paise ELSE 0 END)::bigint AS gross_paise,
                       COALESCE(MAX(s.utr), '')                          AS utr,
                       MAX(s.settled_at)                                 AS settled_at,
                       MIN(sp.created_at)                                AS first_seen_at,
                       COALESCE(MAX(s.status::text), 'processed')        AS status
                FROM rzp_settlement_payments sp
                LEFT JOIN rzp_settlements s ON s.settlement_id = sp.settlement_id
                WHERE sp.merchant_id = $1::uuid
                  AND ($2::date IS NULL OR COALESCE(s.settled_at::date, sp.created_at::date) >= $2::date)
                  AND ($3::date IS NULL OR COALESCE(s.settled_at::date, sp.created_at::date) <= $3::date)
                GROUP BY sp.settlement_id
                """,
                merchant_id, from_date, to_date,
            )
            refunds = await conn.fetch(
                """
                SELECT refund_id, razorpay_payment_id, amount_paise,
                       currency, processed_at, created_at, status::text AS status
                FROM rzp_refunds
                WHERE merchant_id = $1::uuid
                  AND status      IN ('pending', 'processed')
                  AND ($2::date IS NULL OR (COALESCE(processed_at, created_at))::date >= $2::date)
                  AND ($3::date IS NULL OR (COALESCE(processed_at, created_at))::date <= $3::date)
                """,
                merchant_id, from_date, to_date,
            )

        events: list[tuple[datetime, str, dict]] = []

        for p in payments:
            at = p["captured_at"] or p["created_at"]
            gross = _money(p["amount_paise"])
            _mer, commission = _split_gross(gross)
            events.append((at, f"pay:{p['razorpay_payment_id']}", {
                "entry_id": f"pay:{p['razorpay_payment_id']}",
                "type": "CREDIT",
                "source": "payment",
                "amount": float(gross),
                "reference": p["razorpay_payment_id"],
                "order_id": p.get("razorpay_order_id"),
                "at": _iso(at),
                "description": f"Captured payment {p['razorpay_payment_id']}",
                "currency": p["currency"] or "INR",
            }))
            if commission > 0:
                # Anchor 1µs after the credit so it always sorts immediately after.
                at_com = at + timedelta(microseconds=1)
                events.append((at_com, f"com:{p['razorpay_payment_id']}", {
                    "entry_id": f"com:{p['razorpay_payment_id']}",
                    "type": "DEBIT",
                    "source": "commission",
                    "amount": float(commission),
                    "reference": p["razorpay_payment_id"],
                    "order_id": p.get("razorpay_order_id"),
                    "at": _iso(at),
                    "description": "Bittu platform commission (5%)",
                    "currency": p["currency"] or "INR",
                }))

        for s in settlements:
            at = s["settled_at"] or s["first_seen_at"]
            gross = _money(s["gross_paise"])
            merchant_share, _ = _split_gross(gross)
            if merchant_share <= 0:
                continue
            events.append((at, f"setl:{s['settlement_id']}", {
                "entry_id": f"setl:{s['settlement_id']}",
                "type": "DEBIT",
                "source": "settlement",
                "amount": float(merchant_share),
                "reference": s["settlement_id"],
                "utr": s.get("utr") or None,
                "status": s.get("status"),
                "at": _iso(at),
                "description": f"Settled to bank ({s.get('utr') or s['settlement_id']})",
                "currency": "INR",
            }))

        for r in refunds:
            at = r["processed_at"] or r["created_at"]
            amt = _money(r["amount_paise"])
            events.append((at, f"ref:{r['refund_id']}", {
                "entry_id": f"ref:{r['refund_id']}",
                "type": "DEBIT",
                "source": "refund",
                "amount": float(amt),
                "reference": r["refund_id"],
                "payment_id": r["razorpay_payment_id"],
                "at": _iso(at),
                "description": f"Refund {r['refund_id']} for payment {r['razorpay_payment_id']}",
                "currency": r["currency"] or "INR",
            }))

        events.sort(key=lambda t: (t[0], t[1]))

        running = Decimal("0.00")
        out: list[dict] = []
        for _at, _eid, payload in events:
            sign = Decimal("1") if payload["type"] == "CREDIT" else Decimal("-1")
            running = _q(running + sign * Decimal(str(payload["amount"])))
            payload["balance_after"] = float(running)
            out.append(payload)
        return out

    async def list_ledger_entries(
        self,
        merchant_id: str,
        *,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        entry_type: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        merchant_id = _require_merchant(merchant_id)
        stream = await self._build_entries(merchant_id, from_date=from_date, to_date=to_date)
        # API returns newest-first; balance_after was computed on the
        # ascending stream so it remains correct for each row.
        stream_desc = list(reversed(stream))
        if entry_type:
            stream_desc = [e for e in stream_desc if e["type"] == entry_type.upper()]
        if source:
            stream_desc = [e for e in stream_desc if e["source"] == source.lower()]
        total = len(stream_desc)
        return {
            "items": stream_desc[offset: offset + limit],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total,
        }

    async def get_ledger_entry(self, merchant_id: str, entry_id: str) -> dict:
        merchant_id = _require_merchant(merchant_id)
        if ":" not in entry_id:
            raise ValidationError(
                "entry_id must be of the form <prefix>:<razorpay_id> "
                "where prefix is one of pay / com / setl / ref"
            )
        # The full stream is needed for an accurate running balance value
        # on the returned entry. Cap the lookup window in case the merchant
        # has years of history — we walk it all but it's a small payload.
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
            (Decimal(str(e["amount"])) for e in entries if e["type"] == "CREDIT"),
            Decimal("0.00"),
        )
        total_debit = sum(
            (Decimal(str(e["amount"])) for e in entries if e["type"] == "DEBIT"),
            Decimal("0.00"),
        )
        derived = total_credit - total_debit
        if derived < 0:
            derived = Decimal("0.00")
        derived = _q(derived)
        expected = Decimal(str(balance["current_balance"]))
        delta = _q(derived - expected)
        return {
            "merchant_id": merchant_id,
            "consistent": delta == Decimal("0.00"),
            "derived_balance": float(derived),
            "live_balance": float(expected),
            "delta": float(delta),
            "total_credit": float(_q(total_credit)),
            "total_debit": float(_q(total_debit)),
            "entry_count": len(entries),
            "source": "razorpay_local_mirror",
        }


merchant_finance_service = MerchantFinanceService()
