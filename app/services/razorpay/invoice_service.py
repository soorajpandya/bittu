"""
Razorpay Invoices — service layer (Phase 9 deep integration).

Owns ALL writes to ``rzp_invoices``.

Single-write invariants
-----------------------
- Webhooks (``invoice.paid`` / ``partially_paid`` / ``expired``), the
  hourly poller, and the REST surface all funnel through
  ``upsert_invoice_from_razorpay`` — the gateway table is NEVER written
  from anywhere else.
- Merchant resolution priority:
    1. explicit ``merchant_id_override`` (REST / poller / create path)
    2. existing local row binding (``invoice_id`` UNIQUE)
    3. orphan → log + skip (invoices are merchant-scoped at birth;
       there's no platform-UUID fallback because we don't accept
       webhook-only invoices we never created).

Idempotency
-----------
Razorpay idem keys we mint::

    rzp_invoice:{merchant_id}:{receipt|epoch_bucket}        # create
    rzp_invoice_issue:{invoice_id}                          # issue
    rzp_invoice_cancel:{invoice_id}                         # cancel
    rzp_invoice_notify:{invoice_id}:{medium}                # notify
    rzp_invoice_update:{invoice_id}:{rev_hash}              # update

Payment lifecycle (the merchant_ledger credit + escrow hold) is owned
by ``_handle_payment_captured`` in the dispatcher. The invoice handler
ONLY mirrors invoice state; it does NOT re-credit on ``invoice.paid``
because the order linked to the invoice will fire its own
``payment.captured`` webhook independently.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Mapping, Optional, Sequence

from app.core.database import get_connection, get_service_connection
from app.core.logging import get_logger
from app.services.razorpay import invoices as inv_api

logger = get_logger(__name__)


_INVOICE_STATES = (
    "draft", "issued", "partially_paid", "paid", "expired", "cancelled",
)
_TERMINAL_STATES = frozenset({"paid", "expired", "cancelled"})
_MUTABLE_STATES = frozenset({"draft"})


def _coerce_invoice_state(value: Optional[str]) -> str:
    v = (value or "").lower()
    if v in _INVOICE_STATES:
        return v
    # Razorpay occasionally ships transient states; collapse safely.
    if v in {"created"}:
        return "draft"
    if v in {"issued_unpaid"}:
        return "issued"
    return "draft"


def _row_to_invoice(r) -> Optional[dict]:
    if r is None:
        return None
    return {k: v for k, v in dict(r).items() if k != "raw_payload"}


def _epoch_to_ts_arg(value: Any) -> Optional[int]:
    """Return None or int epoch — UPSERT SQL handles the to_timestamp guard."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ════════════════════════════════════════════════════════════════════════
# Service
# ════════════════════════════════════════════════════════════════════════


class RzpInvoiceService:
    # ── Resolution helpers ──────────────────────────────────────────────

    async def _existing_invoice(self, invoice_id: str):
        async with get_service_connection() as conn:
            return await conn.fetchrow(
                "SELECT * FROM rzp_invoices WHERE invoice_id = $1",
                invoice_id,
            )

    async def _resolve_merchant_for_invoice(
        self, invoice_id: str
    ) -> Optional[tuple[str, Optional[str], Optional[str]]]:
        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                "SELECT merchant_id::text         AS merchant_id, "
                "       branch_id::text           AS branch_id, "
                "       internal_order_id::text   AS internal_order_id "
                "FROM rzp_invoices WHERE invoice_id = $1",
                invoice_id,
            )
        if not row:
            return None
        return row["merchant_id"], row["branch_id"], row["internal_order_id"]

    # ── Invoice UPSERT (single write path) ──────────────────────────────

    async def upsert_invoice_from_razorpay(
        self,
        *,
        rzp_entity: Mapping[str, Any],
        merchant_id_override: Optional[str] = None,
        branch_id_override: Optional[str] = None,
        internal_order_id_override: Optional[str] = None,
    ) -> Optional[dict]:
        invoice_id = rzp_entity.get("id")
        if not invoice_id:
            return None

        merchant_id = merchant_id_override
        branch_id = branch_id_override
        internal_order_id = internal_order_id_override
        if not merchant_id:
            resolved = await self._resolve_merchant_for_invoice(invoice_id)
            if resolved:
                merchant_id, existing_branch, existing_order = resolved
                if branch_id is None:
                    branch_id = existing_branch
                if internal_order_id is None:
                    internal_order_id = existing_order
        if not merchant_id:
            logger.warning("rzp_invoice_orphan", invoice_id=invoice_id)
            return None

        local_status = _coerce_invoice_state(rzp_entity.get("status"))

        amount = int(rzp_entity.get("amount") or 0)
        amount_paid = int(rzp_entity.get("amount_paid") or 0)
        amount_due = rzp_entity.get("amount_due")
        if amount_due is None:
            amount_due = max(0, amount - amount_paid)
        amount_due = int(amount_due)

        customer = rzp_entity.get("customer") or {}
        line_items = rzp_entity.get("line_items") or []

        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO rzp_invoices (
                    invoice_id, merchant_id, branch_id, internal_order_id,
                    invoice_number, customer_id, customer_details,
                    amount_paise, amount_paid_paise, amount_due_paise,
                    currency, status, short_url, description,
                    expire_by, issued_at, paid_at, cancelled_at,
                    razorpay_order_id, line_items, raw_payload
                ) VALUES (
                    $1, $2::uuid, $3::uuid, $4::uuid,
                    $5, $6, $7::jsonb,
                    $8, $9, $10,
                    $11, $12::rzp_invoice_state, $13, $14,
                    CASE WHEN $15::bigint IS NULL THEN NULL ELSE to_timestamp($15::bigint) END,
                    CASE WHEN $16::bigint IS NULL THEN NULL ELSE to_timestamp($16::bigint) END,
                    CASE WHEN $17::bigint IS NULL THEN NULL ELSE to_timestamp($17::bigint) END,
                    CASE WHEN $18::bigint IS NULL THEN NULL ELSE to_timestamp($18::bigint) END,
                    $19, $20::jsonb, $21::jsonb
                )
                ON CONFLICT (invoice_id) DO UPDATE SET
                    branch_id           = COALESCE(EXCLUDED.branch_id,           rzp_invoices.branch_id),
                    internal_order_id   = COALESCE(EXCLUDED.internal_order_id,   rzp_invoices.internal_order_id),
                    invoice_number      = COALESCE(EXCLUDED.invoice_number,      rzp_invoices.invoice_number),
                    customer_id         = COALESCE(EXCLUDED.customer_id,         rzp_invoices.customer_id),
                    customer_details    = COALESCE(EXCLUDED.customer_details,    rzp_invoices.customer_details),
                    amount_paise        = EXCLUDED.amount_paise,
                    amount_paid_paise   = GREATEST(EXCLUDED.amount_paid_paise, rzp_invoices.amount_paid_paise),
                    amount_due_paise    = LEAST(EXCLUDED.amount_due_paise, rzp_invoices.amount_due_paise),
                    status              = EXCLUDED.status,
                    short_url           = COALESCE(EXCLUDED.short_url,           rzp_invoices.short_url),
                    description         = COALESCE(EXCLUDED.description,         rzp_invoices.description),
                    expire_by           = COALESCE(EXCLUDED.expire_by,           rzp_invoices.expire_by),
                    issued_at           = COALESCE(EXCLUDED.issued_at,           rzp_invoices.issued_at),
                    paid_at             = COALESCE(EXCLUDED.paid_at,             rzp_invoices.paid_at),
                    cancelled_at        = COALESCE(EXCLUDED.cancelled_at,        rzp_invoices.cancelled_at),
                    razorpay_order_id   = COALESCE(EXCLUDED.razorpay_order_id,   rzp_invoices.razorpay_order_id),
                    line_items          = EXCLUDED.line_items,
                    raw_payload         = EXCLUDED.raw_payload,
                    updated_at          = NOW()
                RETURNING *
                """,
                invoice_id,
                merchant_id,
                branch_id,
                internal_order_id,
                rzp_entity.get("invoice_number") or rzp_entity.get("number"),
                rzp_entity.get("customer_id") or customer.get("id"),
                json.dumps(dict(customer)) if customer else None,
                amount,
                amount_paid,
                amount_due,
                (rzp_entity.get("currency") or "INR")[:3],
                local_status,
                rzp_entity.get("short_url"),
                rzp_entity.get("description"),
                _epoch_to_ts_arg(rzp_entity.get("expire_by")),
                _epoch_to_ts_arg(rzp_entity.get("issued_at")),
                _epoch_to_ts_arg(rzp_entity.get("paid_at")),
                _epoch_to_ts_arg(rzp_entity.get("cancelled_at")),
                rzp_entity.get("order_id"),
                json.dumps(list(line_items)),
                json.dumps(dict(rzp_entity)),
            )
        return _row_to_invoice(row)

    # ── Creation (REST-driven) ──────────────────────────────────────────

    async def create_invoice(
        self,
        *,
        merchant_id: str,
        branch_id: Optional[str] = None,
        internal_order_id: Optional[str] = None,
        amount_paise: int,
        currency: str = "INR",
        customer: Optional[Mapping[str, Any]] = None,
        customer_id: Optional[str] = None,
        description: Optional[str] = None,
        receipt: Optional[str] = None,
        line_items: Optional[Sequence[Mapping[str, Any]]] = None,
        notes: Optional[Mapping[str, Any]] = None,
        sms_notify: bool = True,
        email_notify: bool = True,
        expire_by_epoch: Optional[int] = None,
    ) -> dict:
        if amount_paise < 100:
            raise ValueError("amount_paise must be >= 100")
        if not customer and not customer_id:
            raise ValueError("either customer or customer_id is required")

        bucket = int(time.time()) // 300
        idem_tail = receipt or f"bucket{bucket}"
        idem = f"rzp_invoice:{merchant_id}:{idem_tail}"

        merged_notes = dict(notes or {})
        merged_notes.setdefault("bittu_merchant_id", merchant_id)
        if branch_id:
            merged_notes.setdefault("bittu_branch_id", branch_id)
        if internal_order_id:
            merged_notes.setdefault("bittu_internal_order_id", internal_order_id)

        rzp_resp = await inv_api.create_invoice(
            amount_paise=amount_paise,
            currency=currency,
            customer=customer,
            customer_id=customer_id,
            description=description,
            receipt=receipt,
            line_items=line_items,
            notes=merged_notes,
            sms_notify=sms_notify,
            email_notify=email_notify,
            expire_by=expire_by_epoch,
            idempotency_key=idem,
            merchant_id=merchant_id,
        )

        return await self.upsert_invoice_from_razorpay(
            rzp_entity=rzp_resp,
            merchant_id_override=merchant_id,
            branch_id_override=branch_id,
            internal_order_id_override=internal_order_id,
        )  # type: ignore[return-value]

    # ── State transitions ───────────────────────────────────────────────

    async def issue_invoice(
        self, *, merchant_id: str, invoice_id: str
    ) -> dict:
        await self._verify_ownership(merchant_id, invoice_id)
        rzp_resp = await inv_api.issue_invoice(invoice_id, merchant_id=merchant_id)
        return await self.upsert_invoice_from_razorpay(
            rzp_entity=rzp_resp, merchant_id_override=merchant_id,
        )  # type: ignore[return-value]

    async def cancel_invoice(
        self, *, merchant_id: str, invoice_id: str
    ) -> dict:
        existing = await self._verify_ownership(merchant_id, invoice_id)
        if existing["status"] in _TERMINAL_STATES:
            raise ValueError(f"invoice_in_terminal_state:{existing['status']}")
        rzp_resp = await inv_api.cancel_invoice(invoice_id, merchant_id=merchant_id)
        return await self.upsert_invoice_from_razorpay(
            rzp_entity=rzp_resp, merchant_id_override=merchant_id,
        )  # type: ignore[return-value]

    async def update_invoice(
        self,
        *,
        merchant_id: str,
        invoice_id: str,
        body: Mapping[str, Any],
    ) -> dict:
        existing = await self._verify_ownership(merchant_id, invoice_id)
        if existing["status"] not in _MUTABLE_STATES:
            raise ValueError(f"invoice_not_mutable:{existing['status']}")
        # Razorpay /v1/invoices/{id} doesn't expose a server-side idem key;
        # we still log a deterministic key for audit via the rzp_api_calls table.
        await inv_api.update_invoice(
            invoice_id, body=dict(body), merchant_id=merchant_id,
        )
        rzp_resp = await inv_api.fetch_invoice(invoice_id, merchant_id=merchant_id)
        return await self.upsert_invoice_from_razorpay(
            rzp_entity=rzp_resp, merchant_id_override=merchant_id,
        )  # type: ignore[return-value]

    async def notify_invoice(
        self,
        *,
        merchant_id: str,
        invoice_id: str,
        medium: str,
    ) -> dict:
        if medium not in {"sms", "email"}:
            raise ValueError("medium must be 'sms' or 'email'")
        await self._verify_ownership(merchant_id, invoice_id)
        await inv_api.notify_invoice(
            invoice_id, medium=medium, merchant_id=merchant_id,
        )
        # Notify doesn't change status; just refetch so we capture any
        # delivery-side metadata Razorpay surfaces on the entity.
        rzp_resp = await inv_api.fetch_invoice(invoice_id, merchant_id=merchant_id)
        return await self.upsert_invoice_from_razorpay(
            rzp_entity=rzp_resp, merchant_id_override=merchant_id,
        )  # type: ignore[return-value]

    async def sync_invoice(
        self, *, merchant_id: str, invoice_id: str
    ) -> dict:
        await self._verify_ownership(merchant_id, invoice_id)
        rzp_resp = await inv_api.fetch_invoice(invoice_id, merchant_id=merchant_id)
        return await self.upsert_invoice_from_razorpay(
            rzp_entity=rzp_resp, merchant_id_override=merchant_id,
        )  # type: ignore[return-value]

    # ── Local read APIs ─────────────────────────────────────────────────

    async def get_invoice(
        self, *, merchant_id: str, invoice_id: str
    ) -> Optional[dict]:
        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM rzp_invoices "
                "WHERE merchant_id = $1::uuid AND invoice_id = $2",
                merchant_id, invoice_id,
            )
        return _row_to_invoice(row) if row else None

    async def list_invoices(
        self,
        *,
        merchant_id: str,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        sql = (
            "SELECT * FROM rzp_invoices "
            "WHERE merchant_id = $1::uuid "
        )
        args: list[Any] = [merchant_id]
        if status:
            sql += "AND status = $2::rzp_invoice_state "
            args.append(_coerce_invoice_state(status))
        sql += "ORDER BY created_at DESC LIMIT $%d OFFSET $%d" % (
            len(args) + 1, len(args) + 2,
        )
        args.extend([limit, offset])
        async with get_connection() as conn:
            rows = await conn.fetch(sql, *args)
        return {
            "items": [_row_to_invoice(r) for r in rows],
            "limit": limit,
            "offset": offset,
        }

    # ── Internal ────────────────────────────────────────────────────────

    async def _verify_ownership(
        self, merchant_id: str, invoice_id: str
    ) -> dict:
        existing = await self._existing_invoice(invoice_id)
        if existing is None:
            raise LookupError("invoice_not_found")
        if str(existing["merchant_id"]) != str(merchant_id):
            raise PermissionError("invoice_belongs_to_other_merchant")
        return _row_to_invoice(existing)  # type: ignore[return-value]


rzp_invoice_service = RzpInvoiceService()


# Re-export for downstream callers that mint their own idem keys.
def build_update_idem(invoice_id: str, body: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"rzp_invoice_update:{invoice_id}:{digest}"


__all__ = [
    "rzp_invoice_service",
    "RzpInvoiceService",
    "build_update_idem",
]
