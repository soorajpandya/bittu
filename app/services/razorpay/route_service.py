"""
Razorpay Route service (Phase 7 — linked accounts + transfers).

Single owner of every write to ``rzp_route_accounts`` and ``rzp_route_transfers``.

Design rules (mirror Phase 6 settlements):
- All gateway side-effects go through this service so idempotency keys and
  merchant resolution stay centralised.
- Webhooks/poller/REST all funnel into ``upsert_linked_account_from_razorpay``
  and ``upsert_transfer_from_razorpay`` — never write the gateway tables
  inline anywhere else.
- Linked-account provisioning pulls profile/owner data from
  ``merchant_kyc_*``. Bank details are supplied per-call (the KYC store
  only keeps last4+hash, never the raw account number).
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Optional

from app.core.database import get_connection, get_service_connection
from app.core.logging import get_logger
from app.services.razorpay import route as route_api

logger = get_logger(__name__)


_ACCOUNT_STATES: tuple[str, ...] = (
    "created", "activated", "suspended", "rejected", "deleted",
)
_TRANSFER_STATES: tuple[str, ...] = (
    "created", "processed", "reversed", "failed",
)


def _coerce_account_state(value: Optional[str]) -> str:
    v = (value or "").lower().strip()
    return v if v in _ACCOUNT_STATES else "created"


def _coerce_transfer_state(value: Optional[str]) -> str:
    v = (value or "").lower().strip()
    return v if v in _TRANSFER_STATES else "created"


def _last4(account_number: Optional[str]) -> Optional[str]:
    if not account_number:
        return None
    digits = re.sub(r"\D", "", account_number)
    return digits[-4:] if len(digits) >= 4 else None


def _hash_account(account_number: Optional[str]) -> Optional[str]:
    if not account_number:
        return None
    return hashlib.sha256(account_number.encode("utf-8")).hexdigest()


def _row_to_account(r) -> dict:
    if r is None:
        return {}
    d = dict(r)
    # NEVER expose bank_account_hash via API.
    d.pop("bank_account_hash", None)
    return d


def _row_to_transfer(r) -> dict:
    if r is None:
        return {}
    return dict(r)


class RzpRouteService:
    # ── KYC fetch helpers ───────────────────────────────────────────────

    async def _kyc_snapshot(self, merchant_id: str) -> dict:
        """Pull the bits of KYC needed to provision a linked account."""
        async with get_connection() as conn:
            profile = await conn.fetchrow(
                "SELECT legal_name, business_type, contact_email, contact_phone "
                "FROM merchant_kyc_profiles WHERE merchant_id = $1::uuid",
                merchant_id,
            )
            primary_owner = await conn.fetchrow(
                "SELECT full_name, email, phone "
                "FROM merchant_kyc_owners "
                "WHERE merchant_id = $1::uuid "
                "ORDER BY is_signatory DESC, ownership_pct DESC, id ASC LIMIT 1",
                merchant_id,
            )
            primary_bank = await conn.fetchrow(
                "SELECT account_holder_name, ifsc, bank_name, "
                "       account_number_last4, account_number_hash, is_primary "
                "FROM merchant_kyc_bank_accounts "
                "WHERE merchant_id = $1::uuid AND is_primary = true LIMIT 1",
                merchant_id,
            )
        return {
            "profile": dict(profile) if profile else {},
            "owner": dict(primary_owner) if primary_owner else {},
            "bank":  dict(primary_bank) if primary_bank else {},
        }

    async def _existing_account(self, merchant_id: str):
        async with get_service_connection() as conn:
            return await conn.fetchrow(
                "SELECT * FROM rzp_route_accounts WHERE merchant_id = $1::uuid",
                merchant_id,
            )

    # ── Linked account UPSERT (single write path) ───────────────────────

    async def upsert_linked_account_from_razorpay(
        self,
        *,
        rzp_entity: Mapping[str, Any],
        merchant_id_override: Optional[str] = None,
    ) -> Optional[dict]:
        linked_account_id = rzp_entity.get("id")
        if not linked_account_id:
            return None

        # Resolution priority: explicit override → existing row binding.
        merchant_id = merchant_id_override
        if not merchant_id:
            async with get_service_connection() as conn:
                row = await conn.fetchrow(
                    "SELECT merchant_id::text AS merchant_id FROM rzp_route_accounts "
                    "WHERE linked_account_id = $1",
                    linked_account_id,
                )
            merchant_id = row["merchant_id"] if row else None
        if not merchant_id:
            logger.warning(
                "rzp_route_account_orphan",
                linked_account_id=linked_account_id,
            )
            return None

        profile = (rzp_entity.get("profile") or {}) if isinstance(
            rzp_entity.get("profile"), dict
        ) else {}
        legal_info = (rzp_entity.get("legal_info") or {}) if isinstance(
            rzp_entity.get("legal_info"), dict
        ) else {}

        # Razorpay account state → enum.
        rzp_status = (rzp_entity.get("status") or "").lower()
        if rzp_status in {"under_review", "needs_clarification"}:
            local_status = "created"
        elif rzp_status == "activated":
            local_status = "activated"
        elif rzp_status == "suspended":
            local_status = "suspended"
        elif rzp_status == "rejected":
            local_status = "rejected"
        elif rzp_status == "deleted":
            local_status = "deleted"
        else:
            local_status = _coerce_account_state(rzp_status or None)

        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO rzp_route_accounts (
                    linked_account_id, merchant_id,
                    legal_business_name, business_type,
                    contact_name, email, phone, reference_id,
                    kyc_status, activation_status, status,
                    notes, raw_payload
                ) VALUES (
                    $1, $2::uuid, $3, $4, $5, $6, $7, $8,
                    $9, $10, $11::rzp_route_account_state,
                    COALESCE($12::jsonb, '{}'::jsonb),
                    $13::jsonb
                )
                ON CONFLICT (linked_account_id) DO UPDATE SET
                    legal_business_name = COALESCE(EXCLUDED.legal_business_name, rzp_route_accounts.legal_business_name),
                    business_type       = COALESCE(EXCLUDED.business_type,       rzp_route_accounts.business_type),
                    contact_name        = COALESCE(EXCLUDED.contact_name,        rzp_route_accounts.contact_name),
                    email               = COALESCE(EXCLUDED.email,               rzp_route_accounts.email),
                    phone               = COALESCE(EXCLUDED.phone,               rzp_route_accounts.phone),
                    reference_id        = COALESCE(EXCLUDED.reference_id,        rzp_route_accounts.reference_id),
                    kyc_status          = COALESCE(EXCLUDED.kyc_status,          rzp_route_accounts.kyc_status),
                    activation_status   = COALESCE(EXCLUDED.activation_status,   rzp_route_accounts.activation_status),
                    status              = EXCLUDED.status,
                    raw_payload         = EXCLUDED.raw_payload,
                    updated_at          = NOW()
                RETURNING *
                """,
                linked_account_id, merchant_id,
                rzp_entity.get("legal_business_name") or legal_info.get("business_name"),
                rzp_entity.get("business_type"),
                rzp_entity.get("contact_name") or profile.get("contact_name"),
                rzp_entity.get("email"),
                rzp_entity.get("phone"),
                rzp_entity.get("reference_id"),
                rzp_status or None,
                rzp_entity.get("activation_status"),
                local_status,
                json.dumps(rzp_entity.get("notes") or {}),
                json.dumps(dict(rzp_entity)),
            )
        return _row_to_account(row)

    # ── Provisioning (REST-driven, idempotent) ──────────────────────────

    async def provision_linked_account(
        self,
        *,
        merchant_id: str,
        bank_account_number: Optional[str] = None,
        ifsc_override: Optional[str] = None,
        beneficiary_name_override: Optional[str] = None,
        reference_id: Optional[str] = None,
        extra_notes: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        """
        Create a Razorpay linked account for this merchant if one doesn't
        already exist; otherwise return the existing local row (idempotent).

        The full bank account number is only used in-memory to compute
        last4+hash before being stored; we do NOT persist it.
        """
        existing = await self._existing_account(merchant_id)
        if existing and existing["linked_account_id"]:
            # Re-sync to pick up any out-of-band gateway changes.
            return await self.sync_linked_account(merchant_id=merchant_id)

        snap = await self._kyc_snapshot(merchant_id)
        profile = snap["profile"]
        owner = snap["owner"]
        bank = snap["bank"]

        if not profile.get("legal_name"):
            raise ValueError("KYC profile missing legal_name — cannot provision Route account")
        if not (profile.get("contact_email") or owner.get("email")):
            raise ValueError("KYC profile missing contact email")
        if not (profile.get("contact_phone") or owner.get("phone")):
            raise ValueError("KYC profile missing contact phone")

        contact_name = beneficiary_name_override or owner.get("full_name") or profile.get("legal_name")
        email = profile.get("contact_email") or owner.get("email")
        phone = profile.get("contact_phone") or owner.get("phone")

        notes = {"merchant_id": merchant_id}
        if extra_notes:
            notes.update(dict(extra_notes))

        rzp_resp = await route_api.create_linked_account(
            email=email,
            phone=phone,
            legal_business_name=profile["legal_name"],
            business_type=str(profile.get("business_type") or "individual"),
            contact_name=contact_name,
            profile={
                "category": "food",
                "subcategory": "restaurant",
                "addresses": {},
            },
            notes=notes,
            reference_id=reference_id or f"merchant:{merchant_id}",
            idempotency_key=f"rzp_route_account:{merchant_id}",
            merchant_id=merchant_id,
        )

        # Persist via the single UPSERT path so the row binding lives in
        # exactly one place.
        await self.upsert_linked_account_from_razorpay(
            rzp_entity=rzp_resp, merchant_id_override=merchant_id,
        )

        # Patch in the bank fields locally if a number was provided. We
        # never send the raw number to Razorpay here — that goes through
        # a separate /products call (out of scope for Phase 7).
        last4 = _last4(bank_account_number) or bank.get("account_number_last4")
        bhash = _hash_account(bank_account_number) or bank.get("account_number_hash")
        ifsc = ifsc_override or bank.get("ifsc")
        if last4 or ifsc:
            async with get_service_connection() as conn:
                await conn.execute(
                    """
                    UPDATE rzp_route_accounts
                       SET bank_account_ifsc  = COALESCE($2, bank_account_ifsc),
                           bank_account_last4 = COALESCE($3, bank_account_last4),
                           bank_account_hash  = COALESCE($4, bank_account_hash),
                           updated_at         = NOW()
                     WHERE merchant_id = $1::uuid
                    """,
                    merchant_id, ifsc, last4, bhash,
                )

        return await self.get_linked_account(merchant_id=merchant_id)

    async def sync_linked_account(self, *, merchant_id: str) -> dict:
        existing = await self._existing_account(merchant_id)
        if not existing or not existing["linked_account_id"]:
            raise LookupError("No linked account provisioned for this merchant")
        rzp_resp = await route_api.fetch_linked_account(
            existing["linked_account_id"], merchant_id=merchant_id,
        )
        await self.upsert_linked_account_from_razorpay(
            rzp_entity=rzp_resp, merchant_id_override=merchant_id,
        )
        return await self.get_linked_account(merchant_id=merchant_id)

    async def get_linked_account(self, *, merchant_id: str) -> dict:
        row = await self._existing_account(merchant_id)
        return _row_to_account(row)

    # ── Transfers ───────────────────────────────────────────────────────

    async def _resolve_merchant_for_transfer(
        self, recipient_account_id: Optional[str]
    ) -> Optional[str]:
        if not recipient_account_id:
            return None
        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                "SELECT merchant_id::text AS merchant_id FROM rzp_route_accounts "
                "WHERE linked_account_id = $1",
                recipient_account_id,
            )
        return row["merchant_id"] if row else None

    async def upsert_transfer_from_razorpay(
        self,
        *,
        rzp_entity: Mapping[str, Any],
        merchant_id_override: Optional[str] = None,
        status_override: Optional[str] = None,
    ) -> Optional[dict]:
        transfer_id = rzp_entity.get("id")
        if not transfer_id:
            return None

        recipient = rzp_entity.get("recipient") or rzp_entity.get("recipient_account_id")
        if isinstance(recipient, dict):
            recipient_account_id = recipient.get("id") or recipient.get("account")
        else:
            recipient_account_id = recipient
        if not recipient_account_id and rzp_entity.get("account"):
            recipient_account_id = rzp_entity.get("account")

        merchant_id = (
            merchant_id_override
            or await self._resolve_merchant_for_transfer(recipient_account_id)
        )
        if not merchant_id:
            logger.warning(
                "rzp_transfer_orphan",
                transfer_id=transfer_id,
                recipient_account_id=recipient_account_id,
            )
            # Fall back to platform UUID — recon/poll will promote later.
            merchant_id = "00000000-0000-0000-0000-000000000000"

        status = _coerce_transfer_state(status_override or rzp_entity.get("status"))

        on_hold = bool(rzp_entity.get("on_hold") or False)
        on_hold_until_epoch = rzp_entity.get("on_hold_until")
        processed_at_epoch = (
            rzp_entity.get("processed_at") or rzp_entity.get("created_at")
            if status == "processed" else None
        )
        reversed_at_epoch = rzp_entity.get("reversed_at") if status == "reversed" else None

        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO rzp_route_transfers (
                    transfer_id, razorpay_payment_id,
                    source_account_id, recipient_account_id,
                    merchant_id, amount_paise, currency,
                    on_hold, on_hold_until,
                    fee_paise, tax_paise, status,
                    notes, raw_payload, processed_at, reversed_at
                ) VALUES (
                    $1, $2, $3, $4, $5::uuid, $6, $7,
                    $8,
                    CASE WHEN $9::bigint IS NULL THEN NULL ELSE to_timestamp($9::bigint) END,
                    $10, $11, $12::rzp_route_transfer_state,
                    COALESCE($13::jsonb, '{}'::jsonb),
                    $14::jsonb,
                    CASE WHEN $15::bigint IS NULL THEN NULL ELSE to_timestamp($15::bigint) END,
                    CASE WHEN $16::bigint IS NULL THEN NULL ELSE to_timestamp($16::bigint) END
                )
                ON CONFLICT (transfer_id) DO UPDATE SET
                    razorpay_payment_id  = COALESCE(EXCLUDED.razorpay_payment_id, rzp_route_transfers.razorpay_payment_id),
                    source_account_id    = COALESCE(EXCLUDED.source_account_id,   rzp_route_transfers.source_account_id),
                    recipient_account_id = COALESCE(EXCLUDED.recipient_account_id, rzp_route_transfers.recipient_account_id),
                    merchant_id          = CASE WHEN rzp_route_transfers.merchant_id = '00000000-0000-0000-0000-000000000000'::uuid
                                                THEN EXCLUDED.merchant_id
                                                ELSE rzp_route_transfers.merchant_id END,
                    amount_paise         = EXCLUDED.amount_paise,
                    on_hold              = EXCLUDED.on_hold,
                    on_hold_until        = COALESCE(EXCLUDED.on_hold_until, rzp_route_transfers.on_hold_until),
                    fee_paise            = COALESCE(EXCLUDED.fee_paise, rzp_route_transfers.fee_paise),
                    tax_paise            = COALESCE(EXCLUDED.tax_paise, rzp_route_transfers.tax_paise),
                    status               = EXCLUDED.status,
                    raw_payload          = EXCLUDED.raw_payload,
                    processed_at         = COALESCE(EXCLUDED.processed_at, rzp_route_transfers.processed_at),
                    reversed_at          = COALESCE(EXCLUDED.reversed_at, rzp_route_transfers.reversed_at),
                    updated_at           = NOW()
                RETURNING *
                """,
                transfer_id,
                rzp_entity.get("source") or rzp_entity.get("razorpay_payment_id") or "",
                rzp_entity.get("source_account_id"),
                recipient_account_id,
                merchant_id,
                int(rzp_entity.get("amount") or 0),
                rzp_entity.get("currency") or "INR",
                on_hold,
                int(on_hold_until_epoch) if on_hold_until_epoch else None,
                int(rzp_entity.get("fees") or 0) if rzp_entity.get("fees") is not None else None,
                int(rzp_entity.get("tax") or 0) if rzp_entity.get("tax") is not None else None,
                status,
                json.dumps(rzp_entity.get("notes") or {}),
                json.dumps(dict(rzp_entity)),
                int(processed_at_epoch) if processed_at_epoch else None,
                int(reversed_at_epoch) if reversed_at_epoch else None,
            )
        return _row_to_transfer(row)

    async def create_transfer(
        self,
        *,
        merchant_id: str,
        razorpay_payment_id: str,
        amount_paise: int,
        currency: str = "INR",
        on_hold: bool = False,
        on_hold_until_epoch: Optional[int] = None,
        notes: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        """Split-pay an already-captured payment to this merchant's linked account."""
        acct = await self._existing_account(merchant_id)
        if not acct or not acct["linked_account_id"]:
            raise LookupError("Merchant has no Razorpay linked account")
        if acct["status"] != "activated":
            raise PermissionError(
                f"Linked account not activated (status={acct['status']!r})"
            )

        transfer_body: dict[str, Any] = {
            "account": acct["linked_account_id"],
            "amount": int(amount_paise),
            "currency": currency,
            "notes": dict(notes or {}),
        }
        if on_hold:
            transfer_body["on_hold"] = 1
            if on_hold_until_epoch:
                transfer_body["on_hold_until"] = int(on_hold_until_epoch)

        idem = (
            f"rzp_transfer:{merchant_id}:{razorpay_payment_id}:{int(amount_paise)}"
        )
        rzp_resp = await route_api.create_transfers_for_payment(
            razorpay_payment_id,
            transfers=[transfer_body],
            idempotency_key=idem,
            merchant_id=merchant_id,
        )

        items = (rzp_resp or {}).get("items") or []
        upserted: list[dict] = []
        for item in items:
            item.setdefault("source", razorpay_payment_id)
            row = await self.upsert_transfer_from_razorpay(
                rzp_entity=item, merchant_id_override=merchant_id,
            )
            if row:
                upserted.append(row)
        return {"transfers": upserted, "raw": rzp_resp}

    async def reverse_transfer(
        self,
        *,
        merchant_id: str,
        transfer_id: str,
        amount_paise: Optional[int] = None,
        notes: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        # Sanity: transfer must belong to this merchant.
        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                "SELECT merchant_id::text AS merchant_id, status "
                "FROM rzp_route_transfers WHERE transfer_id = $1",
                transfer_id,
            )
        if not row:
            raise LookupError("transfer not found")
        if row["merchant_id"] != str(merchant_id):
            raise PermissionError("transfer belongs to another merchant")
        if row["status"] in {"reversed", "failed"}:
            raise ValueError(f"transfer already terminal (status={row['status']!r})")

        idem = f"rzp_transfer_reverse:{transfer_id}:{int(amount_paise or 0)}"
        rzp_resp = await route_api.reverse_transfer(
            transfer_id,
            amount_paise=amount_paise,
            notes=dict(notes or {}),
            idempotency_key=idem,
            merchant_id=merchant_id,
        )
        # Refetch the transfer to capture the new state.
        try:
            updated = await route_api.fetch_transfer(transfer_id, merchant_id=merchant_id)
            await self.upsert_transfer_from_razorpay(
                rzp_entity=updated, merchant_id_override=merchant_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("rzp_transfer_refetch_failed", transfer_id=transfer_id)
        return rzp_resp

    async def sync_transfer(self, *, merchant_id: str, transfer_id: str) -> dict:
        rzp_resp = await route_api.fetch_transfer(
            transfer_id, merchant_id=merchant_id,
        )
        row = await self.upsert_transfer_from_razorpay(
            rzp_entity=rzp_resp, merchant_id_override=merchant_id,
        )
        return row or {}

    # ── Local read APIs ─────────────────────────────────────────────────

    async def list_transfers(
        self,
        *,
        merchant_id: str,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        clauses: list[str] = ["merchant_id = $1::uuid"]
        params: list[Any] = [merchant_id]
        if status:
            params.append(status)
            clauses.append(f"status = ${len(params)}::rzp_route_transfer_state")
        params.extend([limit, offset])
        sql = (
            "SELECT * FROM rzp_route_transfers "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC "
            f"LIMIT ${len(params) - 1} OFFSET ${len(params)}"
        )
        async with get_connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [_row_to_transfer(r) for r in rows]

    async def get_transfer(
        self, *, merchant_id: str, transfer_id: str
    ) -> Optional[dict]:
        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM rzp_route_transfers "
                "WHERE transfer_id = $1 AND merchant_id = $2::uuid",
                transfer_id, merchant_id,
            )
        return _row_to_transfer(row) if row else None


rzp_route_service = RzpRouteService()
