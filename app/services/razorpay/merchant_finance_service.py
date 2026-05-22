"""
Razorpay-backed merchant finance projection layer.

Source of truth: the **local Razorpay mirror tables**:

* ``rzp_route_accounts``       — merchant linked-account state (KYC gate)
* ``rzp_route_transfers``      — real net money sent to the merchant's linked
                                  account; the authoritative "what the
                                  merchant has earned" stream.
* ``rzp_settlements``          — Razorpay-side settlement headers (utr,
                                  status, settled_at) for the linked account.
* ``rzp_settlement_payments``  — per-settlement line items (which
                                  payments/refunds/adjustments rolled into
                                  this settlement).
* ``rzp_payments``             — gross payment events (used for the
                                  Transactions tab + gross-sales context).
* ``rzp_refunds``              — refund debits.

Activation model
----------------
A merchant is **Route-active** iff ``rzp_route_accounts.status='activated'``
AND ``linked_account_id IS NOT NULL`` for that ``merchant_id``.

* **Route-active**  → all numbers come from ``rzp_route_transfers``
  (the actual amount Razorpay moved to the linked account). Commission is
  derived as ``payment_amount − transfer_amount`` (real, not synthetic).
* **Pre-onboarded** → wallet endpoints return ``wallet_status='pending_kyc'``
  with every numeric field set to ``0`` (so the frontend never displays a
  fake "withdrawable balance" for an unonboarded merchant). The
  Transactions tab still lists payments (just without a per-row split).

Tenant isolation
----------------
Every query hard-filters by ``merchant_id = $1::uuid``.
``rzp_route_accounts`` is read via ``get_service_connection`` because it
lives outside per-tenant RLS scope; everything else uses the RLS-bound
``get_connection``.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from app.core.database import get_connection, get_service_connection
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)


_Q2 = Decimal("0.01")


# Synthetic split — used ONLY for pre-onboarded merchants where there is no
# transfer row to read from. Once a merchant is Route-active, commission is
# derived from the actual transfer amount, not from this constant.
_LEGACY_COMMISSION_RATE = Decimal("0.05")
_LEGACY_MERCHANT_RATE   = Decimal("0.95")


# Local POS payment methods that bypass Razorpay entirely. Stored in the
# `payments` table; settled the moment they're recorded (no clearing).
_CASH_METHODS = ("cash", "counter", "cod")


# Statement entry categories (spec-mandated names).
ENTRY_PAYMENT_RECEIVED      = "PAYMENT_RECEIVED"
ENTRY_COMMISSION_DEDUCTED   = "COMMISSION_DEDUCTED"
ENTRY_TRANSFER_CREATED      = "TRANSFER_CREATED"
ENTRY_SETTLEMENT_PROCESSED  = "SETTLEMENT_PROCESSED"
ENTRY_REFUND                = "REFUND"


# ─────────────────────────── helpers ──────────────────────────────────────


def _money(paise: Any) -> Decimal:
    """Paise (int) → rupees Decimal(2, ROUND_HALF_UP)."""
    try:
        return (Decimal(int(paise or 0)) / Decimal(100)).quantize(_Q2, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def _q(x: Decimal) -> Decimal:
    return x.quantize(_Q2, rounding=ROUND_HALF_UP)


def _legacy_split(gross_rupees: Decimal) -> tuple[Decimal, Decimal]:
    """Synthetic 95/5 fallback for merchants without a transfer record."""
    if gross_rupees <= 0:
        return Decimal("0.00"), Decimal("0.00")
    merchant = _q(gross_rupees * _LEGACY_MERCHANT_RATE)
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


def _wallet_status_for(account_status: Optional[str]) -> str:
    """Map an rzp_route_accounts.status value to a wallet_status code."""
    if not account_status:
        return "pending_kyc"
    s = account_status.lower()
    if s == "activated":
        return "active"
    if s == "rejected":
        return "kyc_rejected"
    if s == "suspended":
        return "suspended"
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
                "       kyc_status, activation_status "
                "FROM rzp_route_accounts "
                "WHERE merchant_id = $1::uuid",
                merchant_id,
            )
        return dict(row) if row else None

    @staticmethod
    def _is_active(linked: Optional[dict]) -> bool:
        if not linked:
            return False
        return (
            (linked.get("status") or "").lower() == "activated"
            and bool(linked.get("linked_account_id"))
        )

    def _pending_wallet(
        self, merchant_id: str, linked: Optional[dict],
        *, from_date: Optional[date], to_date: Optional[date],
    ) -> dict:
        """Zeroed-out wallet response for pre-onboarded / suspended merchants.

        Every key in the activated response is present so the frontend
        never sees a missing field; only the numbers change.
        """
        return {
            "merchant_id":         merchant_id,
            "wallet_status":       _wallet_status_for((linked or {}).get("status")),
            "kyc_status":          (linked or {}).get("kyc_status"),
            "linked_account_id":   (linked or {}).get("linked_account_id"),
            "gross_sales":         0.0,
            "settled_amount":      0.0,
            "platform_commission": 0.0,
            "pending_settlement":  0.0,
            "refunds":             0.0,
            "available_balance":   0.0,
            "transaction_count":   0,
            "currency":            "INR",
            "window": {
                "from": from_date.isoformat() if from_date else None,
                "to":   to_date.isoformat()   if to_date   else None,
            },
        }

    # ── wallet snapshot ──────────────────────────────────────────────────

    async def wallet_snapshot(
        self,
        merchant_id: str,
        *,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
    ) -> dict:
        merchant_id = _require_merchant(merchant_id)
        linked = await self._linked_account(merchant_id)
        # NOTE: we no longer short-circuit on activation here. Payments can
        # be captured (on the platform account) before the merchant's
        # linked account is fully activated — we still owe them their
        # share. We always project from the local mirror; wallet_status
        # below reports the activation state separately so the FE can
        # decide whether to surface settlement timelines.

        async with get_connection() as conn:
            # Gross captured payments (context only — does NOT contribute
            # to merchant's available balance; what matters is transfers).
            gross_row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(amount_paise), 0)::bigint AS gross_paise,
                       COUNT(*)::bigint                        AS tx_count
                FROM rzp_payments
                WHERE merchant_id = $1::uuid
                  AND status      = 'captured'
                  AND ($2::date IS NULL OR (COALESCE(captured_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date >= $2::date)
                  AND ($3::date IS NULL OR (COALESCE(captured_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3::date)
                """,
                merchant_id, from_date, to_date,
            )
            # Real money that left Bittu master → merchant's linked account.
            transfer_paise = await conn.fetchval(
                """
                SELECT COALESCE(SUM(amount_paise), 0)::bigint
                FROM rzp_route_transfers
                WHERE merchant_id = $1::uuid
                  AND status      IN ('created', 'processed')
                  AND ($2::date IS NULL OR (COALESCE(processed_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date >= $2::date)
                  AND ($3::date IS NULL OR (COALESCE(processed_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3::date)
                """,
                merchant_id, from_date, to_date,
            )
            # Net of merchant share that has been settled (i.e. credit -
            # debit on each line of rzp_settlement_payments for this
            # merchant's linked-account settlements).
            settled_net_paise = await conn.fetchval(
                """
                SELECT COALESCE(SUM(sp.credit_paise - sp.debit_paise), 0)::bigint
                FROM rzp_settlement_payments sp
                LEFT JOIN rzp_settlements s ON s.settlement_id = sp.settlement_id
                WHERE sp.merchant_id = $1::uuid
                  AND COALESCE(s.status::text, 'processed') = 'processed'
                  AND ($2::date IS NULL OR COALESCE((s.settled_at AT TIME ZONE 'Asia/Kolkata')::date, (sp.created_at AT TIME ZONE 'Asia/Kolkata')::date) >= $2::date)
                  AND ($3::date IS NULL OR COALESCE((s.settled_at AT TIME ZONE 'Asia/Kolkata')::date, (sp.created_at AT TIME ZONE 'Asia/Kolkata')::date) <= $3::date)
                """,
                merchant_id, from_date, to_date,
            )
            refund_paise = await conn.fetchval(
                """
                SELECT COALESCE(SUM(amount_paise), 0)::bigint
                FROM rzp_refunds
                WHERE merchant_id = $1::uuid
                  AND status      IN ('pending', 'processed')
                  AND ($2::date IS NULL OR (COALESCE(processed_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date >= $2::date)
                  AND ($3::date IS NULL OR (COALESCE(processed_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3::date)
                """,
                merchant_id, from_date, to_date,
            )
            # Cash / counter / COD payments live in the local `payments`
            # table and never touch Razorpay. They count toward gross
            # sales but bypass settlement entirely (the cash is already
            # in the merchant's till).
            cash_row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(amount), 0)::numeric(14,2) AS cash_amount,
                       COUNT(*)::bigint                         AS cash_count
                FROM payments
                WHERE restaurant_id = $1::uuid
                  AND status        = 'completed'
                  AND LOWER(method)  = ANY($4::text[])
                  AND ($2::date IS NULL OR (COALESCE(paid_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date >= $2::date)
                  AND ($3::date IS NULL OR (COALESCE(paid_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3::date)
                """,
                merchant_id, from_date, to_date, list(_CASH_METHODS),
            )

        online_gross = _money(gross_row["gross_paise"])
        cash_gross   = Decimal(str(cash_row["cash_amount"] or 0)).quantize(_Q2, rounding=ROUND_HALF_UP)
        gross        = online_gross + cash_gross
        transfers    = _money(transfer_paise or 0)
        settled      = _money(settled_net_paise or 0)
        refunds      = _money(refund_paise or 0)

        # Merchant's share of *online* gross is gross minus Bittu's flat
        # 5% commission. Cash never has commission.
        merchant_share_online = (online_gross * Decimal("0.95")).quantize(Decimal("0.01"))
        commission = online_gross - merchant_share_online
        if commission < 0:
            commission = Decimal("0.00")

        # Pending = online money owed to merchant but not yet sent to
        # their bank. Cash is already with them so it is NOT pending.
        pending = merchant_share_online - settled - refunds
        if pending < 0:
            pending = Decimal("0.00")

        # Available = what's already at the linked account but not yet
        # swept to the bank (only meaningful once activation + transfers).
        available = transfers - settled - refunds
        if available < 0:
            available = Decimal("0.00")

        return {
            "merchant_id":         merchant_id,
            "wallet_status":       _wallet_status_for((linked or {}).get("status")),
            "kyc_status":          (linked or {}).get("kyc_status"),
            "linked_account_id":   (linked or {}).get("linked_account_id"),
            "gross_sales":         float(gross),
            "cash_sales":          float(cash_gross),
            "online_sales":        float(online_gross),
            "settled_amount":      float(_q(settled + cash_gross)),
            "platform_commission": float(_q(commission)),
            "pending_settlement":  float(_q(pending)),
            "refunds":             float(refunds),
            "available_balance":   float(_q(available)),
            "transaction_count":   int(gross_row["tx_count"]) + int(cash_row["cash_count"] or 0),
            "currency":            "INR",
            "window": {
                "from": from_date.isoformat() if from_date else None,
                "to":   to_date.isoformat()   if to_date   else None,
            },
        }

    # ── transactions list ────────────────────────────────────────────────

    @staticmethod
    def _project_payment(row: dict) -> dict:
        gross = _money(row["amount_paise"])
        transfer_paise = row.get("transfer_paise")
        transfer_id    = row.get("transfer_id")
        if transfer_paise is not None:
            merchant   = _money(transfer_paise)
            commission = gross - merchant
            if commission < 0:
                commission = Decimal("0.00")
            commission = _q(commission)
        else:
            # Pre-Route fallback (no transfer row yet) — show gross only;
            # don't fabricate a 95/5 split that doesn't match real money.
            merchant   = Decimal("0.00")
            commission = Decimal("0.00")
        notes = _as_dict(row.get("notes"))
        return {
            "transaction_id":   row["razorpay_payment_id"],
            "payment_id":       row["razorpay_payment_id"],
            "transfer_id":      transfer_id,
            "order_id":         row.get("razorpay_order_id"),
            "internal_order_id": (
                str(row["internal_order_id"]) if row.get("internal_order_id") else None
            ),
            "amount":           float(gross),
            "gross_amount":     float(gross),
            "merchant_amount":  float(merchant),
            "commission_amount": float(commission),
            "currency":         row.get("currency") or "INR",
            "status":           row["status"],
            "payment_method":   row.get("method"),
            "method_detail": {
                "vpa":     row.get("upi_vpa"),
                "bank":    row.get("bank_reference"),
                "wallet":  None,
                "card_id": None,
            },
            "customer_email":   notes.get("customer_email"),
            "customer_contact": notes.get("customer_contact"),
            "customer_name":    notes.get("customer_name"),
            "captured":         bool(row.get("captured")),
            "captured_at":      _iso(row.get("captured_at")),
            "created_at":       _iso(row.get("created_at")),
            "fee":              float(_money(row.get("fee_paise"))),
            "tax":              float(_money(row.get("tax_paise"))),
            "description":      notes.get("description"),
            "notes":            notes,
            "error_code":       row.get("error_code"),
            "error_description": row.get("error_description"),
            "source":           "razorpay",
        }

    @staticmethod
    def _project_cash_payment(row: dict) -> dict:
        """Project a row from the local `payments` table (cash / counter /
        COD) into the same shape as a Razorpay capture, so the FE can list
        them on the statement screen uniformly. Cash settles instantly:
        merchant_amount = gross, no commission, no transfer.
        """
        amount = Decimal(str(row.get("amount") or 0)).quantize(_Q2, rounding=ROUND_HALF_UP)
        method = (row.get("method") or "cash").lower()
        pid = str(row.get("id"))
        oid = row.get("order_id")
        return {
            "transaction_id":    pid,
            "payment_id":        pid,
            "transfer_id":       None,
            "order_id":          None,
            "internal_order_id": str(oid) if oid else None,
            "amount":            float(amount),
            "gross_amount":      float(amount),
            "merchant_amount":   float(amount),
            "commission_amount": 0.0,
            "currency":          row.get("currency") or "INR",
            # FE treats anything other than pending/processing/sent_to_bank
            # as "non-pending"; cash is effectively already settled.
            "status":            "settled",
            "settlement_status": "settled",
            "payment_method":    method,
            "method_detail":     {"vpa": None, "bank": None, "wallet": None, "card_id": None},
            "customer_email":    None,
            "customer_contact":  None,
            "customer_name":     None,
            "captured":          True,
            "captured_at":       _iso(row.get("paid_at") or row.get("created_at")),
            "created_at":        _iso(row.get("created_at")),
            "fee":               0.0,
            "tax":               0.0,
            "description":       None,
            "notes":             {},
            "error_code":        None,
            "error_description": None,
            "source":            "cash",
            "channel":           "cash",
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
                  AND ($2::date  IS NULL OR (COALESCE(captured_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date >= $2::date)
                  AND ($3::date  IS NULL OR (COALESCE(captured_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3::date)
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
            # LATERAL pick of the matching transfer for this payment (if
            # any). For pre-onboarded merchants this just returns NULLs and
            # _project_payment falls back to gross-only display.
            rows = await conn.fetch(
                """
                SELECT p.razorpay_payment_id, p.razorpay_order_id, p.internal_order_id,
                       p.amount_paise, p.fee_paise, p.tax_paise, p.currency,
                       p.method, p.upi_vpa, p.bank_reference,
                       p.status::text AS status, p.captured, p.captured_at,
                       p.error_code, p.error_description, p.notes, p.created_at,
                       t.transfer_id, t.amount_paise AS transfer_paise
                FROM rzp_payments p
                LEFT JOIN LATERAL (
                    SELECT transfer_id, amount_paise
                    FROM rzp_route_transfers
                    WHERE razorpay_payment_id = p.razorpay_payment_id
                      AND merchant_id         = p.merchant_id
                      AND status              IN ('created', 'processed')
                    ORDER BY created_at DESC
                    LIMIT 1
                ) t ON TRUE
                WHERE p.merchant_id = $1::uuid
                  AND p.status      = 'captured'
                  AND ($2::date  IS NULL OR (COALESCE(p.captured_at, p.created_at) AT TIME ZONE 'Asia/Kolkata')::date >= $2::date)
                  AND ($3::date  IS NULL OR (COALESCE(p.captured_at, p.created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3::date)
                  AND ($4::text  IS NULL OR p.method = $4::text)
                  AND ($5::bigint IS NULL OR p.amount_paise >= $5::bigint)
                  AND ($6::bigint IS NULL OR p.amount_paise <= $6::bigint)
                  AND ($7::text  IS NULL
                       OR p.razorpay_payment_id ILIKE $7
                       OR p.razorpay_order_id   ILIKE $7
                       OR p.upi_vpa             ILIKE $7
                       OR (p.notes ->> 'customer_email')   ILIKE $7
                       OR (p.notes ->> 'customer_contact') ILIKE $7
                       OR (p.notes ->> 'customer_name')    ILIKE $7)
                ORDER BY COALESCE(p.captured_at, p.created_at) DESC, p.razorpay_payment_id DESC
                LIMIT $8 OFFSET $9
                """,
                merchant_id, from_date, to_date,
                payment_method, min_paise, max_paise, like,
                limit, offset,
            )

            # Also fetch cash / counter / COD payments from the local
            # `payments` table so the statement screen shows them
            # alongside online captures. Skip when caller asked for a
            # Razorpay-specific method (upi/card/netbanking/wallet).
            include_cash = payment_method is None or payment_method.lower() in _CASH_METHODS
            cash_rows: list = []
            cash_total = 0
            if include_cash:
                cash_total = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM payments
                    WHERE restaurant_id = $1::uuid
                      AND status        = 'completed'
                      AND LOWER(method)  = ANY($8::text[])
                      AND ($2::date IS NULL OR (COALESCE(paid_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date >= $2::date)
                      AND ($3::date IS NULL OR (COALESCE(paid_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3::date)
                      AND ($4::text IS NULL OR LOWER(method) = $4::text)
                      AND ($5::bigint IS NULL OR (amount * 100)::bigint >= $5::bigint)
                      AND ($6::bigint IS NULL OR (amount * 100)::bigint <= $6::bigint)
                      AND ($7::text IS NULL OR id::text ILIKE $7 OR order_id::text ILIKE $7)
                    """,
                    merchant_id, from_date, to_date,
                    payment_method, min_paise, max_paise, like,
                    list(_CASH_METHODS),
                ) or 0
                cash_rows = await conn.fetch(
                    """
                    SELECT id, order_id, method, amount, currency, status,
                           paid_at, created_at
                    FROM payments
                    WHERE restaurant_id = $1::uuid
                      AND status        = 'completed'
                      AND LOWER(method)  = ANY($8::text[])
                      AND ($2::date IS NULL OR (COALESCE(paid_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date >= $2::date)
                      AND ($3::date IS NULL OR (COALESCE(paid_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3::date)
                      AND ($4::text IS NULL OR LOWER(method) = $4::text)
                      AND ($5::bigint IS NULL OR (amount * 100)::bigint >= $5::bigint)
                      AND ($6::bigint IS NULL OR (amount * 100)::bigint <= $6::bigint)
                      AND ($7::text IS NULL OR id::text ILIKE $7 OR order_id::text ILIKE $7)
                    ORDER BY COALESCE(paid_at, created_at) DESC
                    LIMIT $9 OFFSET $10
                    """,
                    merchant_id, from_date, to_date,
                    payment_method, min_paise, max_paise, like,
                    list(_CASH_METHODS),
                    limit, offset,
                )

        items = [self._project_payment(dict(r)) for r in rows]
        items.extend(self._project_cash_payment(dict(r)) for r in cash_rows)
        # Newest first across both sources.
        items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return {
            "items":    items,
            "total":    int(total or 0) + int(cash_total or 0),
            "limit":    limit,
            "offset":   offset,
            "has_more": offset + limit < (int(total or 0) + int(cash_total or 0)),
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
                      AND ($3::date IS NULL OR COALESCE((s.settled_at AT TIME ZONE 'Asia/Kolkata')::date, (sp.created_at AT TIME ZONE 'Asia/Kolkata')::date) >= $3::date)
                      AND ($4::date IS NULL OR COALESCE((s.settled_at AT TIME ZONE 'Asia/Kolkata')::date, (sp.created_at AT TIME ZONE 'Asia/Kolkata')::date) <= $4::date)
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
                       COALESCE(SUM(sp.credit_paise - sp.debit_paise), 0)::bigint     AS merchant_paise,
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
                  AND ($3::date IS NULL OR COALESCE((s.settled_at AT TIME ZONE 'Asia/Kolkata')::date, (sp.created_at AT TIME ZONE 'Asia/Kolkata')::date) >= $3::date)
                  AND ($4::date IS NULL OR COALESCE((s.settled_at AT TIME ZONE 'Asia/Kolkata')::date, (sp.created_at AT TIME ZONE 'Asia/Kolkata')::date) <= $4::date)
                GROUP BY sp.settlement_id
                ORDER BY COALESCE(MAX(s.settled_at), MIN(sp.created_at)) DESC NULLS LAST
                LIMIT $5 OFFSET $6
                """,
                merchant_id, status, from_date, to_date, limit, offset,
            )

        items = [self._project_settlement_row(dict(r), merchant_id) for r in rows]
        return {
            "items":    items,
            "total":    int(total or 0),
            "limit":    limit,
            "offset":   offset,
            "has_more": offset + limit < int(total or 0),
        }

    @staticmethod
    def _project_settlement_row(row: dict, merchant_id: str) -> dict:
        gross    = _money(row["gross_paise"])
        refunds  = _money(row["refund_paise"])
        merchant = _money(row.get("merchant_paise") or 0)
        if merchant <= 0:
            # Defensive: if the settlement-line credit/debit columns are
            # zero (older recon rows), fall back to (gross - refunds).
            merchant = gross - refunds
            if merchant < 0:
                merchant = Decimal("0.00")
        commission = gross - refunds - merchant
        if commission < 0:
            commission = Decimal("0.00")
        return {
            "settlement_id":     row["settlement_id"],
            "merchant_id":       merchant_id,
            "gross_amount":      float(_q(gross - refunds)),
            "merchant_amount":   float(_q(merchant)),
            "commission_amount": float(_q(commission)),
            "refund_amount":     float(refunds),
            "fees":              float(_money(row.get("fee_paise"))),
            "tax":               float(_money(row.get("tax_paise"))),
            "transaction_count": int(row.get("tx_count") or 0),
            "utr":               row.get("utr"),
            "status":            row.get("status") or "pending",
            "settled_at":        _iso(row.get("settled_at")),
            "created_at":        _iso(row.get("settled_at") or row.get("first_seen_at")),
            "currency":          "INR",
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
                       COALESCE(SUM(sp.credit_paise - sp.debit_paise), 0)::bigint     AS merchant_paise,
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
                       p.method, p.upi_vpa,
                       t.transfer_id, t.amount_paise AS transfer_paise
                FROM rzp_settlement_payments sp
                LEFT JOIN rzp_payments_index pi
                       ON pi.razorpay_payment_id = sp.razorpay_payment_id
                LEFT JOIN rzp_payments p
                       ON p.id = pi.payment_uuid
                LEFT JOIN LATERAL (
                    SELECT transfer_id, amount_paise
                    FROM rzp_route_transfers
                    WHERE razorpay_payment_id = sp.razorpay_payment_id
                      AND merchant_id         = sp.merchant_id
                      AND status              IN ('created', 'processed')
                    ORDER BY created_at DESC
                    LIMIT 1
                ) t ON TRUE
                WHERE sp.merchant_id  = $1::uuid
                  AND sp.settlement_id = $2
                ORDER BY sp.created_at ASC
                """,
                merchant_id, settlement_id,
            )

        out = self._project_settlement_row(dict(header), merchant_id)
        transfer_ids: list[str] = []
        out["payments"] = []
        for r in lines:
            tid = r.get("transfer_id")
            if tid and tid not in transfer_ids:
                transfer_ids.append(tid)
            out["payments"].append({
                "payment_id":  r["razorpay_payment_id"],
                "transfer_id": tid,
                "type":        r["type"],
                "amount":      float(_money(r["amount_paise"])),
                "fee":         float(_money(r["fee_paise"])),
                "tax":         float(_money(r["tax_paise"])),
                "credit":      float(_money(r["credit_paise"])),
                "debit":       float(_money(r["debit_paise"])),
                "method":      r.get("method"),
                "vpa":         r.get("upi_vpa"),
                "created_at":  _iso(r["created_at"]),
            })
        out["transfer_ids"] = transfer_ids
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
                "status":    st,
                "label":     st.replace("_", " ").title(),
                "completed": done,
                "pending":   pending_flag,
                "at":        anchor if done else None,
            })

        return {
            "settlement_id":  settlement_id,
            "current_status": status,
            "utr":            meta["utr"] if meta else None,
            "events":         events,
            "source":         "razorpay",
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

        buf = io.StringIO()
        w = csv.writer(buf)
        # Spec-mandated columns (TASK 4). Method dropped from spec but
        # retained as the final column so we don't lose information.
        w.writerow([
            "Date", "Payment ID", "Transfer ID", "Settlement ID",
            "Gross Amount", "Commission", "Merchant Amount",
            "Refund", "Status", "UTR", "Method",
        ])

        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT p.razorpay_payment_id           AS payment_id,
                       p.amount_paise                  AS gross_paise,
                       p.status::text                  AS status,
                       COALESCE(p.captured_at, p.created_at) AS at,
                       p.method                        AS method,
                       t.transfer_id                   AS transfer_id,
                       t.amount_paise                  AS transfer_paise,
                       sp.settlement_id                AS settlement_id,
                       s.utr                           AS utr,
                       COALESCE(r.refund_paise, 0)    AS refund_paise
                FROM rzp_payments p
                LEFT JOIN LATERAL (
                    SELECT transfer_id, amount_paise
                    FROM rzp_route_transfers
                    WHERE razorpay_payment_id = p.razorpay_payment_id
                      AND merchant_id         = p.merchant_id
                      AND status              IN ('created', 'processed')
                    ORDER BY created_at DESC
                    LIMIT 1
                ) t ON TRUE
                LEFT JOIN LATERAL (
                    SELECT settlement_id
                    FROM rzp_settlement_payments
                    WHERE razorpay_payment_id = p.razorpay_payment_id
                      AND merchant_id         = p.merchant_id
                      AND type                = 'payment'
                    ORDER BY created_at DESC
                    LIMIT 1
                ) sp ON TRUE
                LEFT JOIN rzp_settlements s
                       ON s.settlement_id = sp.settlement_id
                LEFT JOIN LATERAL (
                    SELECT COALESCE(SUM(amount_paise), 0)::bigint AS refund_paise
                    FROM rzp_refunds
                    WHERE razorpay_payment_id = p.razorpay_payment_id
                      AND merchant_id         = p.merchant_id
                      AND status              IN ('pending', 'processed')
                ) r ON TRUE
                WHERE p.merchant_id = $1::uuid
                  AND p.status      = 'captured'
                  AND (COALESCE(p.captured_at, p.created_at) AT TIME ZONE 'Asia/Kolkata')::date BETWEEN $2::date AND $3::date
                ORDER BY COALESCE(p.captured_at, p.created_at) ASC
                """,
                merchant_id, start, end,
            )

        for r in rows:
            gross   = _money(r["gross_paise"])
            if r.get("transfer_paise") is not None:
                merchant = _money(r["transfer_paise"])
                commission = gross - merchant
                if commission < 0:
                    commission = Decimal("0.00")
            else:
                # No transfer yet — leave both blank to avoid faking a split.
                merchant   = Decimal("0.00")
                commission = Decimal("0.00")
            refund = _money(r["refund_paise"])
            w.writerow([
                _iso(r["at"]) or "",
                r["payment_id"],
                r.get("transfer_id")   or "",
                r.get("settlement_id") or "",
                f"{gross:.2f}",
                f"{_q(commission):.2f}",
                f"{_q(merchant):.2f}",
                f"{refund:.2f}",
                r["status"],
                r.get("utr") or "",
                r.get("method") or "",
            ])

        filename = f"statement_{merchant_id}_{start.isoformat()}_{end.isoformat()}.csv"
        return buf.getvalue(), filename

    # ── ledger balance ───────────────────────────────────────────────────

    async def ledger_balance(self, merchant_id: str) -> dict:
        merchant_id = _require_merchant(merchant_id)
        linked = await self._linked_account(merchant_id)
        if not self._is_active(linked):
            return {
                "merchant_id":        merchant_id,
                "wallet_status":      _wallet_status_for((linked or {}).get("status")),
                "current_balance":    0.0,
                "pending_settlement": 0.0,
                "settled_amount":     0.0,
                "refunded_amount":    0.0,
                "currency":           "INR",
            }

        async with get_connection() as conn:
            transfers = await conn.fetchval(
                """
                SELECT COALESCE(SUM(amount_paise), 0)::bigint
                FROM rzp_route_transfers
                WHERE merchant_id = $1::uuid
                  AND status      IN ('created', 'processed')
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
            settled_net = await conn.fetchval(
                """
                SELECT COALESCE(SUM(sp.credit_paise - sp.debit_paise), 0)::bigint
                FROM rzp_settlement_payments sp
                LEFT JOIN rzp_settlements s ON s.settlement_id = sp.settlement_id
                WHERE sp.merchant_id = $1::uuid
                  AND COALESCE(s.status::text, 'processed') = 'processed'
                """,
                merchant_id,
            )

        transfers_r = _money(transfers or 0)
        settled_r   = _money(settled_net or 0)
        refunds_r   = _money(refunded or 0)

        available = transfers_r - settled_r - refunds_r
        if available < 0:
            available = Decimal("0.00")

        return {
            "merchant_id":        merchant_id,
            "wallet_status":      "active",
            "current_balance":    float(_q(available)),
            "pending_settlement": float(_q(available)),
            "settled_amount":     float(_q(settled_r)),
            "refunded_amount":    float(refunds_r),
            "currency":           "INR",
        }

    # ── ledger entries (statement projection) ────────────────────────────

    async def _build_entries(
        self,
        merchant_id: str,
        *,
        from_date: Optional[date],
        to_date: Optional[date],
    ) -> list[dict]:
        """
        Materialise the merchant's statement as a chronological stream:

          PAYMENT_RECEIVED     (informational, does not move merchant balance)
          COMMISSION_DEDUCTED  (informational, does not move merchant balance)
          TRANSFER_CREATED     (+ balance — real money into linked account)
          SETTLEMENT_PROCESSED (− balance — payout to merchant bank)
          REFUND               (− balance)

        Only ``TRANSFER_CREATED``, ``SETTLEMENT_PROCESSED`` and ``REFUND``
        contribute to ``balance_after`` so the running tally matches what
        the merchant actually owns on the Razorpay side.

        ``PAYMENT_RECEIVED`` and ``COMMISSION_DEDUCTED`` are shown so the
        statement explains *where* a transfer came from, but they don't
        double-count into the running balance.
        """
        async with get_connection() as conn:
            payments = await conn.fetch(
                """
                SELECT p.razorpay_payment_id, p.razorpay_order_id, p.amount_paise,
                       p.currency, p.captured_at, p.created_at,
                       t.transfer_id, t.amount_paise AS transfer_paise
                FROM rzp_payments p
                LEFT JOIN LATERAL (
                    SELECT transfer_id, amount_paise
                    FROM rzp_route_transfers
                    WHERE razorpay_payment_id = p.razorpay_payment_id
                      AND merchant_id         = p.merchant_id
                      AND status              IN ('created', 'processed')
                    ORDER BY created_at DESC
                    LIMIT 1
                ) t ON TRUE
                WHERE p.merchant_id = $1::uuid
                  AND p.status      = 'captured'
                  AND ($2::date IS NULL OR (COALESCE(p.captured_at, p.created_at) AT TIME ZONE 'Asia/Kolkata')::date >= $2::date)
                  AND ($3::date IS NULL OR (COALESCE(p.captured_at, p.created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3::date)
                """,
                merchant_id, from_date, to_date,
            )
            transfers = await conn.fetch(
                """
                SELECT transfer_id, razorpay_payment_id, amount_paise,
                       currency, status::text AS status,
                       processed_at, created_at
                FROM rzp_route_transfers
                WHERE merchant_id = $1::uuid
                  AND status      IN ('created', 'processed')
                  AND ($2::date IS NULL OR (COALESCE(processed_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date >= $2::date)
                  AND ($3::date IS NULL OR (COALESCE(processed_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3::date)
                """,
                merchant_id, from_date, to_date,
            )
            settlements = await conn.fetch(
                """
                SELECT sp.settlement_id,
                       COALESCE(SUM(sp.credit_paise - sp.debit_paise), 0)::bigint AS merchant_paise,
                       COALESCE(MAX(s.utr), '')                                   AS utr,
                       MAX(s.settled_at)                                          AS settled_at,
                       MIN(sp.created_at)                                         AS first_seen_at,
                       COALESCE(MAX(s.status::text), 'processed')                 AS status
                FROM rzp_settlement_payments sp
                LEFT JOIN rzp_settlements s ON s.settlement_id = sp.settlement_id
                WHERE sp.merchant_id = $1::uuid
                  AND ($2::date IS NULL OR COALESCE((s.settled_at AT TIME ZONE 'Asia/Kolkata')::date, (sp.created_at AT TIME ZONE 'Asia/Kolkata')::date) >= $2::date)
                  AND ($3::date IS NULL OR COALESCE((s.settled_at AT TIME ZONE 'Asia/Kolkata')::date, (sp.created_at AT TIME ZONE 'Asia/Kolkata')::date) <= $3::date)
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
                  AND ($2::date IS NULL OR (COALESCE(processed_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date >= $2::date)
                  AND ($3::date IS NULL OR (COALESCE(processed_at, created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3::date)
                """,
                merchant_id, from_date, to_date,
            )

        events: list[tuple[datetime, str, dict]] = []

        # 1. PAYMENT_RECEIVED + COMMISSION_DEDUCTED (informational pair)
        for p in payments:
            at = p["captured_at"] or p["created_at"]
            gross = _money(p["amount_paise"])
            t_paise = p.get("transfer_paise")
            if t_paise is not None:
                merchant_amt = _money(t_paise)
                commission = gross - merchant_amt
                if commission < 0:
                    commission = Decimal("0.00")
            else:
                merchant_amt = Decimal("0.00")
                commission = Decimal("0.00")

            events.append((at, f"pay:{p['razorpay_payment_id']}", {
                "entry_id":        f"pay:{p['razorpay_payment_id']}",
                "entry_type":      ENTRY_PAYMENT_RECEIVED,
                "type":            "CREDIT",
                "source":          "payment",
                "amount":          float(gross),
                "reference":       p["razorpay_payment_id"],
                "order_id":        p.get("razorpay_order_id"),
                "at":              _iso(at),
                "description":     f"Captured payment {p['razorpay_payment_id']}",
                "currency":        p["currency"] or "INR",
                "affects_balance": False,
            }))
            if commission > 0:
                at_com = at + timedelta(microseconds=1)
                events.append((at_com, f"com:{p['razorpay_payment_id']}", {
                    "entry_id":        f"com:{p['razorpay_payment_id']}",
                    "entry_type":      ENTRY_COMMISSION_DEDUCTED,
                    "type":            "DEBIT",
                    "source":          "commission",
                    "amount":          float(_q(commission)),
                    "reference":       p["razorpay_payment_id"],
                    "order_id":        p.get("razorpay_order_id"),
                    "at":              _iso(at_com),
                    "description":     "Platform commission",
                    "currency":        p["currency"] or "INR",
                    "affects_balance": False,
                }))

        # 2. TRANSFER_CREATED — the credit that actually moves the merchant
        #    balance.
        for t in transfers:
            at = t["processed_at"] or t["created_at"]
            amt = _money(t["amount_paise"])
            # Anchor +2µs so transfers sort after the matching commission
            # entry (which is +1µs after the payment).
            events.append((at + timedelta(microseconds=2), f"trf:{t['transfer_id']}", {
                "entry_id":        f"trf:{t['transfer_id']}",
                "entry_type":      ENTRY_TRANSFER_CREATED,
                "type":            "CREDIT",
                "source":          "transfer",
                "amount":          float(amt),
                "reference":       t["transfer_id"],
                "payment_id":      t["razorpay_payment_id"],
                "at":              _iso(at),
                "description":     f"Route transfer {t['transfer_id']}",
                "currency":        t["currency"] or "INR",
                "status":          t.get("status"),
                "affects_balance": True,
            }))

        # 3. SETTLEMENT_PROCESSED — debit when Razorpay sweeps the linked
        #    account balance to the merchant's bank.
        for s in settlements:
            at = s["settled_at"] or s["first_seen_at"]
            amt = _money(s["merchant_paise"])
            if amt <= 0:
                continue
            events.append((at, f"setl:{s['settlement_id']}", {
                "entry_id":        f"setl:{s['settlement_id']}",
                "entry_type":      ENTRY_SETTLEMENT_PROCESSED,
                "type":            "DEBIT",
                "source":          "settlement",
                "amount":          float(amt),
                "reference":       s["settlement_id"],
                "utr":             s.get("utr") or None,
                "status":          s.get("status"),
                "at":              _iso(at),
                "description":     f"Settled to bank ({s.get('utr') or s['settlement_id']})",
                "currency":        "INR",
                "affects_balance": True,
            }))

        # 4. REFUND — debit when a refund is initiated/processed.
        for r in refunds:
            at = r["processed_at"] or r["created_at"]
            amt = _money(r["amount_paise"])
            events.append((at, f"ref:{r['refund_id']}", {
                "entry_id":        f"ref:{r['refund_id']}",
                "entry_type":      ENTRY_REFUND,
                "type":            "DEBIT",
                "source":          "refund",
                "amount":          float(amt),
                "reference":       r["refund_id"],
                "payment_id":      r["razorpay_payment_id"],
                "at":              _iso(at),
                "description":     f"Refund {r['refund_id']} for payment {r['razorpay_payment_id']}",
                "currency":        r["currency"] or "INR",
                "affects_balance": True,
            }))

        events.sort(key=lambda t: (t[0], t[1]))

        running = Decimal("0.00")
        out: list[dict] = []
        for _at, _eid, payload in events:
            if payload.get("affects_balance"):
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
        linked = await self._linked_account(merchant_id)
        if not self._is_active(linked):
            return {
                "items":         [],
                "total":         0,
                "limit":         limit,
                "offset":        offset,
                "has_more":      False,
                "wallet_status": _wallet_status_for((linked or {}).get("status")),
            }

        stream = await self._build_entries(merchant_id, from_date=from_date, to_date=to_date)
        # Newest-first; balance_after computed on ascending stream so each
        # row still carries the correct running tally.
        stream_desc = list(reversed(stream))
        if entry_type:
            wanted = entry_type.upper()
            stream_desc = [
                e for e in stream_desc
                if e["type"] == wanted or e.get("entry_type") == wanted
            ]
        if source:
            stream_desc = [e for e in stream_desc if e["source"] == source.lower()]
        total = len(stream_desc)
        return {
            "items":    stream_desc[offset: offset + limit],
            "total":    total,
            "limit":    limit,
            "offset":   offset,
            "has_more": offset + limit < total,
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
            (Decimal(str(e["amount"])) for e in entries
             if e["type"] == "CREDIT" and e.get("affects_balance")),
            Decimal("0.00"),
        )
        total_debit = sum(
            (Decimal(str(e["amount"])) for e in entries
             if e["type"] == "DEBIT" and e.get("affects_balance")),
            Decimal("0.00"),
        )
        derived = total_credit - total_debit
        if derived < 0:
            derived = Decimal("0.00")
        derived = _q(derived)
        expected = Decimal(str(balance["current_balance"]))
        delta = _q(derived - expected)
        return {
            "merchant_id":   merchant_id,
            "consistent":    delta == Decimal("0.00"),
            "derived_balance": float(derived),
            "live_balance":  float(expected),
            "delta":         float(delta),
            "total_credit":  float(_q(total_credit)),
            "total_debit":   float(_q(total_debit)),
            "entry_count":   len(entries),
            "source":        "razorpay_route_mirror",
        }


merchant_finance_service = MerchantFinanceService()
