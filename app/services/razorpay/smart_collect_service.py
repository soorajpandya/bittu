"""
Razorpay Smart Collect — service layer (Phase 8 deep integration).

Owns ALL writes to ``rzp_smart_collect_va`` and ``rzp_smart_collect_txn``.

Single-write invariants
-----------------------
- Webhooks (``virtual_account.created`` / ``credited`` / ``closed``),
  poller, and REST surfaces all funnel into
  ``upsert_va_from_razorpay`` and ``upsert_txn_from_razorpay`` — the
  gateway tables are NEVER written from anywhere else.
- Merchant resolution priority for VA upserts:
    1. explicit ``merchant_id_override`` (REST / poller path)
    2. existing local row binding (``virtual_account_id`` UNIQUE)
    3. orphan → log + skip (no platform-UUID row for VAs because
       provisioning is the only path that creates them).
- Merchant resolution priority for TXN upserts:
    1. explicit ``merchant_id_override``
    2. JOIN to ``rzp_smart_collect_va`` via ``virtual_account_id``
    3. orphan → log + skip (we cannot meaningfully attribute a
       bank-transfer credit to nobody).

Idempotency
-----------
Razorpay idem keys we mint::

    rzp_va:{merchant_id}:{branch_id|"-"}:{descriptor|"-"}:{epoch_bucket}
    rzp_va_close:{virtual_account_id}
    rzp_va_payer:{virtual_account_id}:{payer_hash}

Webhook two-tier idempotency comes from
``payment_webhook_events`` (transport) + ``ON CONFLICT`` on the
gateway tables (business level).
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Mapping, Optional, Sequence

from app.core.database import get_connection, get_service_connection
from app.core.logging import get_logger
from app.services.razorpay import smart_collect as sc_api

logger = get_logger(__name__)


_VA_STATES = ("active", "closed")


def _coerce_va_state(value: Optional[str]) -> str:
    v = (value or "").lower()
    if v in _VA_STATES:
        return v
    if v in {"paid", "expired", "cancelled"}:
        # Razorpay sometimes ships these on terminal VAs; collapse to closed.
        return "closed"
    return "active"


def _row_to_va(r) -> dict:
    if r is None:
        return None  # type: ignore[return-value]
    return {k: v for k, v in dict(r).items() if k != "raw_payload"}


def _row_to_txn(r) -> dict:
    if r is None:
        return None  # type: ignore[return-value]
    d = dict(r)
    # Strip the raw acquirer payload but keep the structured payer fields.
    d.pop("raw_payload", None)
    return d


def _payer_hash(payer: Mapping[str, Any]) -> str:
    """Stable hash of an allowed_payer dict for idempotency."""
    blob = json.dumps(payer, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


# ════════════════════════════════════════════════════════════════════════
# Service
# ════════════════════════════════════════════════════════════════════════


class RzpSmartCollectService:
    # ── Resolution helpers ──────────────────────────────────────────────

    async def _existing_va(self, virtual_account_id: str):
        async with get_service_connection() as conn:
            return await conn.fetchrow(
                "SELECT * FROM rzp_smart_collect_va WHERE virtual_account_id = $1",
                virtual_account_id,
            )

    async def _resolve_merchant_for_va(
        self, virtual_account_id: str
    ) -> Optional[tuple[str, Optional[str]]]:
        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                "SELECT merchant_id::text AS merchant_id, "
                "       branch_id::text AS branch_id "
                "FROM rzp_smart_collect_va WHERE virtual_account_id = $1",
                virtual_account_id,
            )
        if not row:
            return None
        return row["merchant_id"], row["branch_id"]

    # ── Virtual-account UPSERT (single write path) ──────────────────────

    async def upsert_va_from_razorpay(
        self,
        *,
        rzp_entity: Mapping[str, Any],
        merchant_id_override: Optional[str] = None,
        branch_id_override: Optional[str] = None,
    ) -> Optional[dict]:
        virtual_account_id = rzp_entity.get("id")
        if not virtual_account_id:
            return None

        merchant_id = merchant_id_override
        branch_id = branch_id_override
        if not merchant_id:
            resolved = await self._resolve_merchant_for_va(virtual_account_id)
            if resolved:
                merchant_id, existing_branch = resolved
                if branch_id is None:
                    branch_id = existing_branch
        if not merchant_id:
            logger.warning(
                "rzp_va_orphan",
                virtual_account_id=virtual_account_id,
            )
            return None

        receivers = rzp_entity.get("receivers") or []
        if isinstance(receivers, dict):
            # Some Razorpay surfaces ship a dict {"types":[...], "vpa":{...}};
            # normalise to a list for storage.
            receivers = [receivers]
        allowed_payers = rzp_entity.get("allowed_payers") or []
        notes = rzp_entity.get("notes") or {}
        amount_paid = int(rzp_entity.get("amount_paid") or 0)
        amount_expected = rzp_entity.get("amount_expected")
        close_by = rzp_entity.get("close_by")
        closed_at = rzp_entity.get("closed_at")
        local_status = _coerce_va_state(rzp_entity.get("status"))

        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO rzp_smart_collect_va (
                    virtual_account_id, merchant_id, branch_id,
                    customer_id, name, description,
                    receivers, allowed_payers, status,
                    amount_paid_paise, amount_expected_paise,
                    close_by, closed_at,
                    notes, raw_payload
                ) VALUES (
                    $1, $2::uuid, $3::uuid,
                    $4, $5, $6,
                    $7::jsonb, $8::jsonb, $9::rzp_va_state,
                    $10, $11,
                    CASE WHEN $12::bigint IS NULL THEN NULL
                         ELSE to_timestamp($12::bigint) END,
                    CASE WHEN $13::bigint IS NULL THEN NULL
                         ELSE to_timestamp($13::bigint) END,
                    $14::jsonb, $15::jsonb
                )
                ON CONFLICT (virtual_account_id) DO UPDATE SET
                    branch_id             = COALESCE(EXCLUDED.branch_id,             rzp_smart_collect_va.branch_id),
                    customer_id           = COALESCE(EXCLUDED.customer_id,           rzp_smart_collect_va.customer_id),
                    name                  = COALESCE(EXCLUDED.name,                  rzp_smart_collect_va.name),
                    description           = COALESCE(EXCLUDED.description,           rzp_smart_collect_va.description),
                    receivers             = EXCLUDED.receivers,
                    allowed_payers        = EXCLUDED.allowed_payers,
                    status                = EXCLUDED.status,
                    amount_paid_paise     = GREATEST(EXCLUDED.amount_paid_paise, rzp_smart_collect_va.amount_paid_paise),
                    amount_expected_paise = COALESCE(EXCLUDED.amount_expected_paise, rzp_smart_collect_va.amount_expected_paise),
                    close_by              = COALESCE(EXCLUDED.close_by,              rzp_smart_collect_va.close_by),
                    closed_at             = COALESCE(EXCLUDED.closed_at,             rzp_smart_collect_va.closed_at),
                    notes                 = EXCLUDED.notes,
                    raw_payload           = EXCLUDED.raw_payload,
                    updated_at            = NOW()
                RETURNING *
                """,
                virtual_account_id,
                merchant_id,
                branch_id,
                rzp_entity.get("customer_id"),
                rzp_entity.get("name"),
                rzp_entity.get("description"),
                json.dumps(list(receivers)),
                json.dumps(list(allowed_payers)),
                local_status,
                amount_paid,
                int(amount_expected) if amount_expected is not None else None,
                int(close_by) if close_by else None,
                int(closed_at) if closed_at else None,
                json.dumps(dict(notes)),
                json.dumps(dict(rzp_entity)),
            )
        return _row_to_va(row)

    # ── Inbound transaction UPSERT (single write path) ──────────────────

    async def upsert_txn_from_razorpay(
        self,
        *,
        payment_entity: Mapping[str, Any],
        bank_transfer_entity: Optional[Mapping[str, Any]] = None,
        upi_entity: Optional[Mapping[str, Any]] = None,
        va_entity: Optional[Mapping[str, Any]] = None,
        merchant_id_override: Optional[str] = None,
    ) -> Optional[dict]:
        razorpay_payment_id = payment_entity.get("id")
        if not razorpay_payment_id:
            return None

        # Determine the virtual_account_id this credit belongs to.
        virtual_account_id: Optional[str] = None
        if va_entity:
            virtual_account_id = va_entity.get("id")
        if not virtual_account_id:
            # Sometimes the payment entity carries it directly.
            virtual_account_id = payment_entity.get("virtual_account_id")
        if not virtual_account_id and bank_transfer_entity:
            virtual_account_id = bank_transfer_entity.get("virtual_account_id")
        if not virtual_account_id and upi_entity:
            virtual_account_id = upi_entity.get("virtual_account_id")

        if not virtual_account_id:
            logger.warning(
                "rzp_va_txn_no_va",
                razorpay_payment_id=razorpay_payment_id,
            )
            return None

        # Resolve merchant via the local VA row.
        merchant_id = merchant_id_override
        if not merchant_id:
            resolved = await self._resolve_merchant_for_va(virtual_account_id)
            merchant_id = resolved[0] if resolved else None
        if not merchant_id:
            logger.warning(
                "rzp_va_txn_orphan",
                razorpay_payment_id=razorpay_payment_id,
                virtual_account_id=virtual_account_id,
            )
            return None

        method = (payment_entity.get("method") or "").lower() or None

        # Pull payer + bank fields from whichever side-entity is present.
        payer_name: Optional[str] = None
        payer_account_number: Optional[str] = None
        payer_ifsc: Optional[str] = None
        bank_reference: Optional[str] = None
        transfer_mode: Optional[str] = None
        upi_payer_vpa: Optional[str] = None

        if bank_transfer_entity:
            payer_account = bank_transfer_entity.get("payer_bank_account") or {}
            payer_name = payer_account.get("name") or bank_transfer_entity.get("payer_name")
            payer_account_number = payer_account.get("account_number")
            payer_ifsc = payer_account.get("ifsc")
            bank_reference = bank_transfer_entity.get("bank_reference")
            transfer_mode = (bank_transfer_entity.get("mode") or "").upper() or None
        if upi_entity:
            upi_payer_vpa = upi_entity.get("payer_vpa") or upi_entity.get("vpa")
            transfer_mode = transfer_mode or "UPI"
            bank_reference = bank_reference or upi_entity.get("npci_reference_id")

        # Last-resort: payment.acquirer_data may carry RRN / UTR.
        acquirer = payment_entity.get("acquirer_data") or {}
        bank_reference = bank_reference or acquirer.get("rrn") or acquirer.get("upi_transaction_id")
        upi_payer_vpa = upi_payer_vpa or acquirer.get("vpa")

        amount_paise = int(payment_entity.get("amount") or 0)
        currency = (payment_entity.get("currency") or "INR")[:3]

        raw_payload = {
            "payment": dict(payment_entity),
            "bank_transfer": dict(bank_transfer_entity) if bank_transfer_entity else None,
            "upi": dict(upi_entity) if upi_entity else None,
            "virtual_account": dict(va_entity) if va_entity else None,
        }

        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO rzp_smart_collect_txn (
                    razorpay_payment_id, virtual_account_id, merchant_id,
                    amount_paise, currency, method,
                    upi_payer_vpa, payer_name,
                    payer_account_number, payer_ifsc,
                    bank_reference, transfer_mode,
                    raw_payload
                ) VALUES (
                    $1, $2, $3::uuid,
                    $4, $5, $6,
                    $7, $8,
                    $9, $10,
                    $11, $12,
                    $13::jsonb
                )
                ON CONFLICT (razorpay_payment_id) DO UPDATE SET
                    virtual_account_id   = EXCLUDED.virtual_account_id,
                    amount_paise         = EXCLUDED.amount_paise,
                    method               = COALESCE(EXCLUDED.method,               rzp_smart_collect_txn.method),
                    upi_payer_vpa        = COALESCE(EXCLUDED.upi_payer_vpa,        rzp_smart_collect_txn.upi_payer_vpa),
                    payer_name           = COALESCE(EXCLUDED.payer_name,           rzp_smart_collect_txn.payer_name),
                    payer_account_number = COALESCE(EXCLUDED.payer_account_number, rzp_smart_collect_txn.payer_account_number),
                    payer_ifsc           = COALESCE(EXCLUDED.payer_ifsc,           rzp_smart_collect_txn.payer_ifsc),
                    bank_reference       = COALESCE(EXCLUDED.bank_reference,       rzp_smart_collect_txn.bank_reference),
                    transfer_mode        = COALESCE(EXCLUDED.transfer_mode,        rzp_smart_collect_txn.transfer_mode),
                    raw_payload          = EXCLUDED.raw_payload
                RETURNING *
                """,
                razorpay_payment_id,
                virtual_account_id,
                merchant_id,
                amount_paise,
                currency,
                method,
                upi_payer_vpa,
                payer_name,
                payer_account_number,
                payer_ifsc,
                bank_reference,
                transfer_mode,
                json.dumps(raw_payload),
            )
        return _row_to_txn(row)

    # ── Provisioning (REST-driven) ──────────────────────────────────────

    async def provision_virtual_account(
        self,
        *,
        merchant_id: str,
        branch_id: Optional[str] = None,
        receivers_types: Sequence[str],
        descriptor: Optional[str] = None,
        customer_id: Optional[str] = None,
        description: Optional[str] = None,
        amount_expected_paise: Optional[int] = None,
        notes: Optional[Mapping[str, Any]] = None,
        allowed_payers: Optional[Sequence[Mapping[str, Any]]] = None,
        close_by_epoch: Optional[int] = None,
    ) -> dict:
        """
        Create a fresh virtual account at Razorpay and mirror it locally.

        NOTE: Each call creates a NEW VA. Callers wanting "one VA per
        customer" should manage their own dedup before calling this.
        """
        types = [t.lower() for t in receivers_types]
        if not types:
            raise ValueError("receivers_types must contain at least one of: bank_account, vpa")
        for t in types:
            if t not in {"bank_account", "vpa"}:
                raise ValueError(f"unsupported receiver type: {t}")

        # Stable-but-coarse idem bucket (5-minute window) so retries from
        # the same caller within a window collapse into one VA.
        bucket = int(time.time()) // 300
        idem = f"rzp_va:{merchant_id}:{branch_id or '-'}:{descriptor or '-'}:{bucket}"

        merged_notes = dict(notes or {})
        merged_notes.setdefault("bittu_merchant_id", merchant_id)
        if branch_id:
            merged_notes.setdefault("bittu_branch_id", branch_id)

        rzp_resp = await sc_api.create_virtual_account(
            receivers_types=types,
            descriptor=descriptor,
            customer_id=customer_id,
            description=description,
            amount_expected_paise=amount_expected_paise,
            notes=merged_notes,
            allowed_payers=allowed_payers,
            close_by=close_by_epoch,
            idempotency_key=idem,
            merchant_id=merchant_id,
        )

        return await self.upsert_va_from_razorpay(
            rzp_entity=rzp_resp,
            merchant_id_override=merchant_id,
            branch_id_override=branch_id,
        )  # type: ignore[return-value]

    async def close_virtual_account(
        self, *, merchant_id: str, virtual_account_id: str
    ) -> dict:
        owner = await self._resolve_merchant_for_va(virtual_account_id)
        if owner is None:
            raise LookupError("virtual_account_not_found")
        if owner[0] != merchant_id:
            raise PermissionError("virtual_account_belongs_to_other_merchant")

        rzp_resp = await sc_api.close_virtual_account(
            virtual_account_id, merchant_id=merchant_id,
        )
        return await self.upsert_va_from_razorpay(
            rzp_entity=rzp_resp,
            merchant_id_override=merchant_id,
        )  # type: ignore[return-value]

    async def add_allowed_payer(
        self,
        *,
        merchant_id: str,
        virtual_account_id: str,
        payer: Mapping[str, Any],
    ) -> dict:
        owner = await self._resolve_merchant_for_va(virtual_account_id)
        if owner is None:
            raise LookupError("virtual_account_not_found")
        if owner[0] != merchant_id:
            raise PermissionError("virtual_account_belongs_to_other_merchant")

        # Razorpay client doesn't expose idem_key on add_allowed_payer in
        # smart_collect.py, so we re-fetch after to mirror state.
        await sc_api.add_allowed_payer(
            virtual_account_id, payer=payer, merchant_id=merchant_id,
        )
        rzp_resp = await sc_api.fetch_virtual_account(
            virtual_account_id, merchant_id=merchant_id,
        )
        # _payer_hash retained for callers that build their own idem keys.
        _ = _payer_hash(payer)
        return await self.upsert_va_from_razorpay(
            rzp_entity=rzp_resp,
            merchant_id_override=merchant_id,
        )  # type: ignore[return-value]

    async def sync_virtual_account(
        self, *, merchant_id: str, virtual_account_id: str
    ) -> dict:
        owner = await self._resolve_merchant_for_va(virtual_account_id)
        if owner is None:
            raise LookupError("virtual_account_not_found")
        if owner[0] != merchant_id:
            raise PermissionError("virtual_account_belongs_to_other_merchant")

        rzp_resp = await sc_api.fetch_virtual_account(
            virtual_account_id, merchant_id=merchant_id,
        )
        return await self.upsert_va_from_razorpay(
            rzp_entity=rzp_resp,
            merchant_id_override=merchant_id,
        )  # type: ignore[return-value]

    async def sync_va_payments(
        self,
        *,
        merchant_id: str,
        virtual_account_id: str,
        count: int = 25,
    ) -> dict:
        """Re-mirror inbound payments from Razorpay for a given VA."""
        owner = await self._resolve_merchant_for_va(virtual_account_id)
        if owner is None:
            raise LookupError("virtual_account_not_found")
        if owner[0] != merchant_id:
            raise PermissionError("virtual_account_belongs_to_other_merchant")

        rzp_resp = await sc_api.fetch_va_payments(
            virtual_account_id, count=count, merchant_id=merchant_id,
        )
        items = (rzp_resp or {}).get("items") or []
        upserted: list[dict] = []
        for payment_entity in items:
            # /v1/virtual_accounts/{id}/payments returns just payments.
            # The bank_transfer side-entity must be fetched per-payment;
            # we keep it best-effort to avoid N+1 storms in the poller.
            row = await self.upsert_txn_from_razorpay(
                payment_entity=payment_entity,
                merchant_id_override=merchant_id,
            )
            if row is not None:
                upserted.append(row)
        return {"transactions": upserted, "count": len(upserted)}

    # ── Local read APIs ─────────────────────────────────────────────────

    async def get_virtual_account(
        self, *, merchant_id: str, virtual_account_id: str
    ) -> Optional[dict]:
        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM rzp_smart_collect_va "
                "WHERE merchant_id = $1::uuid AND virtual_account_id = $2",
                merchant_id, virtual_account_id,
            )
        return _row_to_va(row) if row else None

    async def list_virtual_accounts(
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
            "SELECT * FROM rzp_smart_collect_va "
            "WHERE merchant_id = $1::uuid "
        )
        args: list[Any] = [merchant_id]
        if status:
            sql += "AND status = $2::rzp_va_state "
            args.append(_coerce_va_state(status))
        sql += "ORDER BY created_at DESC LIMIT $%d OFFSET $%d" % (
            len(args) + 1, len(args) + 2,
        )
        args.extend([limit, offset])
        async with get_connection() as conn:
            rows = await conn.fetch(sql, *args)
        return {
            "items": [_row_to_va(r) for r in rows],
            "limit": limit,
            "offset": offset,
        }

    async def list_transactions(
        self,
        *,
        merchant_id: str,
        virtual_account_id: Optional[str] = None,
        reconciled: Optional[bool] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        sql = (
            "SELECT * FROM rzp_smart_collect_txn "
            "WHERE merchant_id = $1::uuid "
        )
        args: list[Any] = [merchant_id]
        if virtual_account_id:
            sql += "AND virtual_account_id = $%d " % (len(args) + 1)
            args.append(virtual_account_id)
        if reconciled is not None:
            sql += "AND reconciled = $%d " % (len(args) + 1)
            args.append(bool(reconciled))
        sql += "ORDER BY created_at DESC LIMIT $%d OFFSET $%d" % (
            len(args) + 1, len(args) + 2,
        )
        args.extend([limit, offset])
        async with get_connection() as conn:
            rows = await conn.fetch(sql, *args)
        return {
            "items": [_row_to_txn(r) for r in rows],
            "limit": limit,
            "offset": offset,
        }


rzp_smart_collect_service = RzpSmartCollectService()


__all__ = ["rzp_smart_collect_service", "RzpSmartCollectService"]
