"""
Merchant KYC service — Phase 9.

Self-contained merchant-level KYC engine. Independent of the existing
user-level Cashfree-driven `kyc_verifications` table. NO gateway wiring.

State model lives in `merchant_kyc_profiles.status`:
    draft → submitted → under_review → {approved, rejected}
    approved   → suspended → under_review (re-review)
    rejected   → draft (resubmit) — implicit via update + submit
    suspended  → approved (unsuspend)

Documents, owners, bank accounts are sub-resources scoped to merchant_id.
All state transitions go through `fn_kyc_*` SQL functions which write an
append-only `merchant_kyc_audit_events` row inside the same txn.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional
from uuid import UUID

from app.core.database import get_connection
from app.core.exceptions import NotFoundError, ValidationError, ConflictError
from app.core.logging import get_logger

logger = get_logger(__name__)

_BUSINESS_TYPES = {
    "proprietorship", "partnership", "llp", "private_limited",
    "public_limited", "huf", "trust", "society", "individual", "other",
}
_DOC_TYPES = {
    "pan_card", "gstin_certificate", "coi", "moa", "aoa",
    "address_proof", "bank_proof",
    "owner_id_proof", "owner_address_proof",
    "partnership_deed", "shop_license", "other",
}
_DOC_STATUSES = {"pending", "verified", "rejected", "expired"}
_OWNER_ROLES = {"director", "partner", "proprietor", "ubo", "authorized_signatory"}

_PAN_RE   = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
_IFSC_RE  = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
_ACCT_RE  = re.compile(r"^[0-9]{6,18}$")


# ───────────────────────────── row mappers ─────────────────────────────
def _profile_row(r) -> dict:
    if r is None:
        return {}
    addr = r["registered_address"]
    if isinstance(addr, str):
        addr = json.loads(addr)
    meta = r["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    return {
        "merchant_id":          str(r["merchant_id"]),
        "legal_name":           r["legal_name"],
        "business_type":        r["business_type"],
        "pan":                  r["pan"],
        "gstin":                r["gstin"],
        "cin":                  r["cin"],
        "registered_address":   addr or {},
        "contact_email":        r["contact_email"],
        "contact_phone":        r["contact_phone"],
        "website":              r["website"],
        "status":               r["status"],
        "risk_tier":            r["risk_tier"],
        "rejection_reason":     r["rejection_reason"],
        "suspension_reason":    r["suspension_reason"],
        "submitted_at":         r["submitted_at"].isoformat() if r["submitted_at"] else None,
        "reviewed_at":          r["reviewed_at"].isoformat() if r["reviewed_at"] else None,
        "reviewed_by_admin_id": str(r["reviewed_by_admin_id"]) if r["reviewed_by_admin_id"] else None,
        "approved_at":          r["approved_at"].isoformat() if r["approved_at"] else None,
        "suspended_at":         r["suspended_at"].isoformat() if r["suspended_at"] else None,
        "version":              int(r["version"]),
        "metadata":             meta or {},
        "created_at":           r["created_at"].isoformat(),
        "updated_at":           r["updated_at"].isoformat(),
    }


def _doc_row(r) -> dict:
    meta = r["metadata"]
    if isinstance(meta, str): meta = json.loads(meta)
    return {
        "id":                   int(r["id"]),
        "document_uuid":        str(r["document_uuid"]),
        "merchant_id":          str(r["merchant_id"]),
        "owner_id":             int(r["owner_id"]) if r["owner_id"] else None,
        "doc_type":             r["doc_type"],
        "file_url":             r["file_url"],
        "file_hash":            r["file_hash"],
        "mime_type":            r["mime_type"],
        "size_bytes":           int(r["size_bytes"]) if r["size_bytes"] else None,
        "status":               r["status"],
        "rejection_reason":     r["rejection_reason"],
        "expires_at":           r["expires_at"].isoformat() if r["expires_at"] else None,
        "uploaded_by_user_id":  str(r["uploaded_by_user_id"]) if r["uploaded_by_user_id"] else None,
        "verified_by_admin_id": str(r["verified_by_admin_id"]) if r["verified_by_admin_id"] else None,
        "verified_at":          r["verified_at"].isoformat() if r["verified_at"] else None,
        "metadata":             meta or {},
        "created_at":           r["created_at"].isoformat(),
        "updated_at":           r["updated_at"].isoformat(),
    }


def _owner_row(r) -> dict:
    meta = r["metadata"]
    if isinstance(meta, str): meta = json.loads(meta)
    return {
        "id":            int(r["id"]),
        "owner_uuid":    str(r["owner_uuid"]),
        "merchant_id":   str(r["merchant_id"]),
        "full_name":     r["full_name"],
        "role":          r["role"],
        "dob":           r["dob"].isoformat() if r["dob"] else None,
        "pan":           r["pan"],
        "aadhaar_last4": r["aadhaar_last4"],
        "ownership_pct": str(r["ownership_pct"]),
        "email":         r["email"],
        "phone":         r["phone"],
        "is_signatory":  bool(r["is_signatory"]),
        "metadata":      meta or {},
        "created_at":    r["created_at"].isoformat(),
        "updated_at":    r["updated_at"].isoformat(),
    }


def _bank_row(r) -> dict:
    meta = r["metadata"]
    if isinstance(meta, str): meta = json.loads(meta)
    return {
        "id":                   int(r["id"]),
        "account_uuid":         str(r["account_uuid"]),
        "merchant_id":          str(r["merchant_id"]),
        "account_holder_name":  r["account_holder_name"],
        "account_number_last4": r["account_number_last4"],
        # account_number_hash deliberately omitted from API surface
        "ifsc":                 r["ifsc"],
        "bank_name":            r["bank_name"],
        "branch":               r["branch"],
        "account_type":         r["account_type"],
        "is_primary":           bool(r["is_primary"]),
        "is_verified":          bool(r["is_verified"]),
        "verification_method":  r["verification_method"],
        "verification_ref":     r["verification_ref"],
        "verified_by_admin_id": str(r["verified_by_admin_id"]) if r["verified_by_admin_id"] else None,
        "verified_at":          r["verified_at"].isoformat() if r["verified_at"] else None,
        "metadata":             meta or {},
        "created_at":           r["created_at"].isoformat(),
        "updated_at":           r["updated_at"].isoformat(),
    }


def _audit_row(r) -> dict:
    payload = r["payload"]
    if isinstance(payload, str): payload = json.loads(payload)
    return {
        "id":             int(r["id"]),
        "merchant_id":    str(r["merchant_id"]),
        "event_type":     r["event_type"],
        "from_status":    r["from_status"],
        "to_status":      r["to_status"],
        "actor_user_id":  str(r["actor_user_id"]) if r["actor_user_id"] else None,
        "actor_admin_id": str(r["actor_admin_id"]) if r["actor_admin_id"] else None,
        "reason":         r["reason"],
        "payload":        payload or {},
        "created_at":     r["created_at"].isoformat(),
    }


class KYCService:
    # ╔════════════════════════════════════════════════════════════════╗
    # ║ Profile                                                        ║
    # ╚════════════════════════════════════════════════════════════════╝
    async def get_profile(self, merchant_id: str | UUID) -> dict:
        async with get_connection() as c:
            r = await c.fetchrow(
                "SELECT * FROM merchant_kyc_profiles WHERE merchant_id = $1::uuid",
                str(merchant_id),
            )
        if not r:
            raise NotFoundError("kyc profile not found")
        return _profile_row(r)

    async def get_or_create_profile(self, merchant_id: str | UUID) -> dict:
        async with get_connection() as c:
            r = await c.fetchrow(
                "SELECT * FROM merchant_kyc_profiles WHERE merchant_id = $1::uuid",
                str(merchant_id),
            )
            if not r:
                r = await c.fetchrow(
                    """
                    INSERT INTO merchant_kyc_profiles (merchant_id)
                    VALUES ($1::uuid)
                    RETURNING *
                    """,
                    str(merchant_id),
                )
        return _profile_row(r)

    async def update_profile(
        self,
        merchant_id: str | UUID,
        *,
        legal_name: Optional[str] = None,
        business_type: Optional[str] = None,
        pan: Optional[str] = None,
        gstin: Optional[str] = None,
        cin: Optional[str] = None,
        registered_address: Optional[dict] = None,
        contact_email: Optional[str] = None,
        contact_phone: Optional[str] = None,
        website: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        # Read current to enforce editability
        prof = await self.get_or_create_profile(merchant_id)
        if prof["status"] not in ("draft", "rejected", "suspended"):
            raise ConflictError(
                f"profile is {prof['status']}; not editable"
            )

        if business_type is not None and business_type not in _BUSINESS_TYPES:
            raise ValidationError(f"business_type must be one of {sorted(_BUSINESS_TYPES)}")
        if pan is not None and not _PAN_RE.match(pan):
            raise ValidationError("pan must match AAAAA9999A")
        if gstin is not None and gstin and not _GSTIN_RE.match(gstin):
            raise ValidationError("gstin format invalid")

        sets, params = [], []
        def _add(col, val, cast=""):
            params.append(val)
            sets.append(f"{col} = ${len(params)}{cast}")

        if legal_name        is not None: _add("legal_name", legal_name)
        if business_type     is not None: _add("business_type", business_type, "::merchant_kyc_business_type")
        if pan               is not None: _add("pan", pan)
        if gstin             is not None: _add("gstin", gstin or None)
        if cin               is not None: _add("cin", cin or None)
        if registered_address is not None: _add("registered_address", json.dumps(registered_address), "::jsonb")
        if contact_email     is not None: _add("contact_email", contact_email)
        if contact_phone     is not None: _add("contact_phone", contact_phone)
        if website           is not None: _add("website", website or None)
        if metadata          is not None: _add("metadata", json.dumps(metadata), "::jsonb")

        if not sets:
            return prof

        params.append(str(merchant_id))
        async with get_connection() as c:
            r = await c.fetchrow(
                f"""
                UPDATE merchant_kyc_profiles
                   SET {', '.join(sets)}
                 WHERE merchant_id = ${len(params)}::uuid
                 RETURNING *
                """,
                *params,
            )
        return _profile_row(r)

    # ╔════════════════════════════════════════════════════════════════╗
    # ║ Documents                                                      ║
    # ╚════════════════════════════════════════════════════════════════╝
    async def add_document(
        self,
        merchant_id: str | UUID,
        *,
        doc_type: str,
        file_url: str,
        mime_type: Optional[str] = None,
        size_bytes: Optional[int] = None,
        file_hash: Optional[str] = None,
        owner_id: Optional[int] = None,
        expires_at: Optional[str] = None,
        uploaded_by_user_id: Optional[str | UUID] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        if doc_type not in _DOC_TYPES:
            raise ValidationError(f"doc_type must be one of {sorted(_DOC_TYPES)}")
        if not file_url or not file_url.strip():
            raise ValidationError("file_url required")
        if file_hash is None:
            file_hash = hashlib.sha256(file_url.encode()).hexdigest()

        # ensure profile exists
        await self.get_or_create_profile(merchant_id)

        async with get_connection() as c:
            r = await c.fetchrow(
                """
                INSERT INTO merchant_kyc_documents
                  (merchant_id, owner_id, doc_type, file_url, file_hash,
                   mime_type, size_bytes, expires_at, uploaded_by_user_id, metadata)
                VALUES ($1::uuid, $2, $3::merchant_kyc_doc_type, $4, $5,
                        $6, $7, $8::timestamptz, $9::uuid, $10::jsonb)
                RETURNING *
                """,
                str(merchant_id),
                owner_id,
                doc_type,
                file_url,
                file_hash,
                mime_type,
                size_bytes,
                expires_at,
                str(uploaded_by_user_id) if uploaded_by_user_id else None,
                json.dumps(metadata or {}),
            )
            await c.execute(
                """
                INSERT INTO merchant_kyc_audit_events
                  (merchant_id, event_type, actor_user_id, payload)
                VALUES ($1::uuid, 'doc.uploaded', $2::uuid, $3::jsonb)
                """,
                str(merchant_id),
                str(uploaded_by_user_id) if uploaded_by_user_id else None,
                json.dumps({"document_id": int(r["id"]), "doc_type": doc_type}),
            )
        return _doc_row(r)

    async def list_documents(
        self,
        merchant_id: Optional[str | UUID] = None,
        *,
        doc_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        clauses, params = [], []
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        if doc_type is not None:
            if doc_type not in _DOC_TYPES:
                raise ValidationError("invalid doc_type")
            params.append(doc_type)
            clauses.append(f"doc_type = ${len(params)}::merchant_kyc_doc_type")
        if status is not None:
            if status not in _DOC_STATUSES:
                raise ValidationError("invalid status")
            params.append(status)
            clauses.append(f"status = ${len(params)}::merchant_kyc_doc_status")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])
        async with get_connection() as c:
            rows = await c.fetch(
                f"""
                SELECT * FROM merchant_kyc_documents
                {where}
                ORDER BY id DESC
                LIMIT ${len(params) - 1} OFFSET ${len(params)}
                """,
                *params,
            )
        return [_doc_row(r) for r in rows]

    async def verify_document(
        self,
        document_id: int,
        *,
        admin_id: str | UUID,
        merchant_id: Optional[str | UUID] = None,
    ) -> dict:
        async with get_connection() as c:
            doc = await self._fetch_doc(c, document_id, merchant_id)
            if doc["status"] not in ("pending", "expired"):
                raise ConflictError(f"document is {doc['status']}; cannot verify")
            r = await c.fetchrow(
                """
                UPDATE merchant_kyc_documents
                   SET status = 'verified',
                       verified_by_admin_id = $1::uuid,
                       verified_at = now(),
                       rejection_reason = NULL
                 WHERE id = $2
                 RETURNING *
                """,
                str(admin_id), document_id,
            )
            await c.execute(
                """
                INSERT INTO merchant_kyc_audit_events
                  (merchant_id, event_type, actor_admin_id, payload)
                VALUES ($1::uuid, 'doc.verified', $2::uuid, $3::jsonb)
                """,
                str(r["merchant_id"]), str(admin_id),
                json.dumps({"document_id": document_id}),
            )
        return _doc_row(r)

    async def reject_document(
        self,
        document_id: int,
        *,
        admin_id: str | UUID,
        reason: str,
        merchant_id: Optional[str | UUID] = None,
    ) -> dict:
        if not reason or not reason.strip():
            raise ValidationError("reason required")
        async with get_connection() as c:
            doc = await self._fetch_doc(c, document_id, merchant_id)
            if doc["status"] == "rejected":
                raise ConflictError("document already rejected")
            r = await c.fetchrow(
                """
                UPDATE merchant_kyc_documents
                   SET status = 'rejected',
                       verified_by_admin_id = $1::uuid,
                       verified_at = now(),
                       rejection_reason = $2
                 WHERE id = $3
                 RETURNING *
                """,
                str(admin_id), reason, document_id,
            )
            await c.execute(
                """
                INSERT INTO merchant_kyc_audit_events
                  (merchant_id, event_type, actor_admin_id, reason, payload)
                VALUES ($1::uuid, 'doc.rejected', $2::uuid, $3, $4::jsonb)
                """,
                str(r["merchant_id"]), str(admin_id), reason,
                json.dumps({"document_id": document_id}),
            )
        return _doc_row(r)

    async def delete_document(
        self,
        document_id: int,
        *,
        merchant_id: str | UUID,
        actor_user_id: Optional[str | UUID] = None,
    ) -> None:
        async with get_connection() as c:
            doc = await self._fetch_doc(c, document_id, merchant_id)
            # only deletable while profile is editable
            prof = await c.fetchrow(
                "SELECT status FROM merchant_kyc_profiles WHERE merchant_id = $1::uuid",
                str(merchant_id),
            )
            if prof and prof["status"] not in ("draft", "rejected"):
                raise ConflictError(f"profile is {prof['status']}; documents locked")
            await c.execute("DELETE FROM merchant_kyc_documents WHERE id = $1", document_id)
            await c.execute(
                """
                INSERT INTO merchant_kyc_audit_events
                  (merchant_id, event_type, actor_user_id, payload)
                VALUES ($1::uuid, 'doc.deleted', $2::uuid, $3::jsonb)
                """,
                str(merchant_id),
                str(actor_user_id) if actor_user_id else None,
                json.dumps({"document_id": document_id, "doc_type": doc["doc_type"]}),
            )

    async def _fetch_doc(self, c, document_id: int, merchant_id) -> dict:
        if merchant_id is not None:
            r = await c.fetchrow(
                "SELECT * FROM merchant_kyc_documents WHERE id = $1 AND merchant_id = $2::uuid",
                document_id, str(merchant_id),
            )
        else:
            r = await c.fetchrow(
                "SELECT * FROM merchant_kyc_documents WHERE id = $1",
                document_id,
            )
        if not r:
            raise NotFoundError("document not found")
        return _doc_row(r)

    # ╔════════════════════════════════════════════════════════════════╗
    # ║ Owners                                                         ║
    # ╚════════════════════════════════════════════════════════════════╝
    async def add_owner(
        self,
        merchant_id: str | UUID,
        *,
        full_name: str,
        role: str,
        dob: Optional[str] = None,
        pan: Optional[str] = None,
        aadhaar_last4: Optional[str] = None,
        ownership_pct: float = 0,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        is_signatory: bool = False,
        metadata: Optional[dict] = None,
    ) -> dict:
        if role not in _OWNER_ROLES:
            raise ValidationError(f"role must be one of {sorted(_OWNER_ROLES)}")
        if not full_name or not full_name.strip():
            raise ValidationError("full_name required")
        if pan and not _PAN_RE.match(pan):
            raise ValidationError("pan must match AAAAA9999A")
        if aadhaar_last4 and (len(aadhaar_last4) != 4 or not aadhaar_last4.isdigit()):
            raise ValidationError("aadhaar_last4 must be 4 digits")
        if not (0 <= float(ownership_pct) <= 100):
            raise ValidationError("ownership_pct must be 0..100")

        await self.get_or_create_profile(merchant_id)

        async with get_connection() as c:
            r = await c.fetchrow(
                """
                INSERT INTO merchant_kyc_owners
                  (merchant_id, full_name, role, dob, pan, aadhaar_last4,
                   ownership_pct, email, phone, is_signatory, metadata)
                VALUES ($1::uuid, $2, $3::merchant_kyc_owner_role, $4::date, $5, $6,
                        $7, $8, $9, $10, $11::jsonb)
                RETURNING *
                """,
                str(merchant_id), full_name, role, dob, pan, aadhaar_last4,
                ownership_pct, email, phone, is_signatory,
                json.dumps(metadata or {}),
            )
        return _owner_row(r)

    async def list_owners(self, merchant_id: str | UUID) -> list[dict]:
        async with get_connection() as c:
            rows = await c.fetch(
                "SELECT * FROM merchant_kyc_owners WHERE merchant_id = $1::uuid ORDER BY id",
                str(merchant_id),
            )
        return [_owner_row(r) for r in rows]

    async def remove_owner(
        self, owner_id: int, *, merchant_id: str | UUID,
        actor_user_id: Optional[str | UUID] = None,
    ) -> None:
        async with get_connection() as c:
            r = await c.fetchrow(
                "SELECT * FROM merchant_kyc_owners WHERE id = $1 AND merchant_id = $2::uuid",
                owner_id, str(merchant_id),
            )
            if not r:
                raise NotFoundError("owner not found")
            prof = await c.fetchrow(
                "SELECT status FROM merchant_kyc_profiles WHERE merchant_id = $1::uuid",
                str(merchant_id),
            )
            if prof and prof["status"] not in ("draft", "rejected"):
                raise ConflictError(f"profile is {prof['status']}; owners locked")
            await c.execute("DELETE FROM merchant_kyc_owners WHERE id = $1", owner_id)
            await c.execute(
                """
                INSERT INTO merchant_kyc_audit_events
                  (merchant_id, event_type, actor_user_id, payload)
                VALUES ($1::uuid, 'owner.removed', $2::uuid, $3::jsonb)
                """,
                str(merchant_id),
                str(actor_user_id) if actor_user_id else None,
                json.dumps({"owner_id": owner_id}),
            )

    # ╔════════════════════════════════════════════════════════════════╗
    # ║ Bank accounts                                                  ║
    # ╚════════════════════════════════════════════════════════════════╝
    async def add_bank_account(
        self,
        merchant_id: str | UUID,
        *,
        account_holder_name: str,
        account_number: str,
        ifsc: str,
        bank_name: Optional[str] = None,
        branch: Optional[str] = None,
        account_type: str = "current",
        is_primary: bool = False,
        metadata: Optional[dict] = None,
    ) -> dict:
        if not _ACCT_RE.match(account_number):
            raise ValidationError("account_number must be 6..18 digits")
        if not _IFSC_RE.match(ifsc):
            raise ValidationError("ifsc format invalid (AAAA0XXXXXX)")
        if account_type not in ("savings", "current", "nro", "nre"):
            raise ValidationError("account_type invalid")

        last4 = account_number[-4:]
        acct_hash = hashlib.sha256(account_number.encode()).hexdigest()

        await self.get_or_create_profile(merchant_id)

        async with get_connection() as c:
            # if requested primary, demote existing primary to satisfy unique partial idx
            if is_primary:
                await c.execute(
                    """
                    UPDATE merchant_kyc_bank_accounts
                       SET is_primary = false
                     WHERE merchant_id = $1::uuid AND is_primary = true
                    """,
                    str(merchant_id),
                )
            else:
                # if this is the first account, force primary
                cnt = await c.fetchval(
                    "SELECT COUNT(*) FROM merchant_kyc_bank_accounts WHERE merchant_id = $1::uuid",
                    str(merchant_id),
                )
                if cnt == 0:
                    is_primary = True

            r = await c.fetchrow(
                """
                INSERT INTO merchant_kyc_bank_accounts
                  (merchant_id, account_holder_name, account_number_last4,
                   account_number_hash, ifsc, bank_name, branch,
                   account_type, is_primary, metadata)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
                RETURNING *
                """,
                str(merchant_id), account_holder_name, last4, acct_hash,
                ifsc.upper(), bank_name, branch, account_type, is_primary,
                json.dumps(metadata or {}),
            )
            await c.execute(
                """
                INSERT INTO merchant_kyc_audit_events
                  (merchant_id, event_type, payload)
                VALUES ($1::uuid, 'bank.added', $2::jsonb)
                """,
                str(merchant_id),
                json.dumps({"bank_account_id": int(r["id"]), "is_primary": is_primary}),
            )
        return _bank_row(r)

    async def list_bank_accounts(
        self, merchant_id: Optional[str | UUID] = None
    ) -> list[dict]:
        async with get_connection() as c:
            if merchant_id is not None:
                rows = await c.fetch(
                    "SELECT * FROM merchant_kyc_bank_accounts WHERE merchant_id = $1::uuid "
                    "ORDER BY is_primary DESC, id",
                    str(merchant_id),
                )
            else:
                rows = await c.fetch(
                    "SELECT * FROM merchant_kyc_bank_accounts "
                    "ORDER BY merchant_id, is_primary DESC, id"
                )
        return [_bank_row(r) for r in rows]

    async def set_primary_bank(
        self, bank_id: int, *, merchant_id: str | UUID,
    ) -> dict:
        async with get_connection() as c:
            r = await c.fetchrow(
                "SELECT * FROM merchant_kyc_bank_accounts WHERE id = $1 AND merchant_id = $2::uuid",
                bank_id, str(merchant_id),
            )
            if not r:
                raise NotFoundError("bank account not found")
            if not r["is_verified"]:
                raise ConflictError("bank account must be verified before promoting to primary")
            await c.execute(
                "UPDATE merchant_kyc_bank_accounts SET is_primary = false "
                "WHERE merchant_id = $1::uuid AND is_primary = true",
                str(merchant_id),
            )
            r2 = await c.fetchrow(
                "UPDATE merchant_kyc_bank_accounts SET is_primary = true "
                "WHERE id = $1 RETURNING *",
                bank_id,
            )
        return _bank_row(r2)

    async def verify_bank_account(
        self,
        bank_id: int,
        *,
        admin_id: str | UUID,
        method: str = "manual",
        reference: Optional[str] = None,
        merchant_id: Optional[str | UUID] = None,
    ) -> dict:
        if method not in ("penny_drop", "doc", "manual"):
            raise ValidationError("verification method invalid")
        async with get_connection() as c:
            if merchant_id is not None:
                r = await c.fetchrow(
                    "SELECT * FROM merchant_kyc_bank_accounts WHERE id = $1 AND merchant_id = $2::uuid",
                    bank_id, str(merchant_id),
                )
            else:
                r = await c.fetchrow(
                    "SELECT * FROM merchant_kyc_bank_accounts WHERE id = $1",
                    bank_id,
                )
            if not r:
                raise NotFoundError("bank account not found")
            r2 = await c.fetchrow(
                """
                UPDATE merchant_kyc_bank_accounts
                   SET is_verified = true,
                       verification_method = $1,
                       verification_ref = $2,
                       verified_by_admin_id = $3::uuid,
                       verified_at = now()
                 WHERE id = $4
                 RETURNING *
                """,
                method, reference, str(admin_id), bank_id,
            )
            await c.execute(
                """
                INSERT INTO merchant_kyc_audit_events
                  (merchant_id, event_type, actor_admin_id, payload)
                VALUES ($1::uuid, 'bank.verified', $2::uuid, $3::jsonb)
                """,
                str(r2["merchant_id"]), str(admin_id),
                json.dumps({"bank_account_id": bank_id, "method": method}),
            )
        return _bank_row(r2)

    # ╔════════════════════════════════════════════════════════════════╗
    # ║ Lifecycle (FSM via SQL fns)                                    ║
    # ╚════════════════════════════════════════════════════════════════╝
    async def submit(
        self, merchant_id: str | UUID, *, actor_user_id: str | UUID
    ) -> dict:
        try:
            async with get_connection() as c:
                j = await c.fetchval(
                    "SELECT fn_kyc_submit($1::uuid, $2::uuid)",
                    str(merchant_id), str(actor_user_id),
                )
        except Exception as e:
            msg = str(e)
            if "incomplete" in msg or "cannot submit" in msg:
                raise ValidationError(msg)
            if "not found" in msg:
                raise NotFoundError(msg)
            raise
        return self._jsonb_to_profile(j)

    async def set_under_review(
        self, merchant_id: str | UUID, *, admin_id: str | UUID
    ) -> dict:
        try:
            async with get_connection() as c:
                j = await c.fetchval(
                    "SELECT fn_kyc_set_under_review($1::uuid, $2::uuid)",
                    str(merchant_id), str(admin_id),
                )
        except Exception as e:
            msg = str(e)
            if "not found" in msg:  raise NotFoundError(msg)
            if "cannot move" in msg: raise ConflictError(msg)
            raise
        return self._jsonb_to_profile(j)

    async def review(
        self,
        merchant_id: str | UUID,
        *,
        admin_id: str | UUID,
        decision: str,
        reason: Optional[str] = None,
    ) -> dict:
        if decision not in ("approve", "reject"):
            raise ValidationError("decision must be approve|reject")
        try:
            async with get_connection() as c:
                j = await c.fetchval(
                    "SELECT fn_kyc_review($1::uuid, $2::uuid, $3, $4)",
                    str(merchant_id), str(admin_id), decision, reason,
                )
        except Exception as e:
            msg = str(e)
            if "not found" in msg: raise NotFoundError(msg)
            if "cannot review" in msg or "rejection requires" in msg:
                raise ConflictError(msg) if "cannot review" in msg else ValidationError(msg)
            raise
        return self._jsonb_to_profile(j)

    async def suspend(
        self, merchant_id: str | UUID, *, admin_id: str | UUID, reason: str
    ) -> dict:
        try:
            async with get_connection() as c:
                j = await c.fetchval(
                    "SELECT fn_kyc_suspend($1::uuid, $2::uuid, $3)",
                    str(merchant_id), str(admin_id), reason,
                )
        except Exception as e:
            msg = str(e)
            if "not found" in msg: raise NotFoundError(msg)
            if "suspension requires" in msg: raise ValidationError(msg)
            if "can only suspend" in msg: raise ConflictError(msg)
            raise
        return self._jsonb_to_profile(j)

    async def unsuspend(
        self, merchant_id: str | UUID, *, admin_id: str | UUID
    ) -> dict:
        try:
            async with get_connection() as c:
                j = await c.fetchval(
                    "SELECT fn_kyc_unsuspend($1::uuid, $2::uuid)",
                    str(merchant_id), str(admin_id),
                )
        except Exception as e:
            msg = str(e)
            if "not found" in msg: raise NotFoundError(msg)
            if "can only unsuspend" in msg: raise ConflictError(msg)
            raise
        return self._jsonb_to_profile(j)

    @staticmethod
    def _jsonb_to_profile(j) -> dict:
        if j is None:
            return {}
        if isinstance(j, str):
            j = json.loads(j)
        # convert ISO-ish timestamps as-is; matches _profile_row shape
        return {
            "merchant_id":          j.get("merchant_id"),
            "legal_name":           j.get("legal_name"),
            "business_type":        j.get("business_type"),
            "pan":                  j.get("pan"),
            "gstin":                j.get("gstin"),
            "cin":                  j.get("cin"),
            "registered_address":   j.get("registered_address") or {},
            "contact_email":        j.get("contact_email"),
            "contact_phone":        j.get("contact_phone"),
            "website":              j.get("website"),
            "status":               j.get("status"),
            "risk_tier":            j.get("risk_tier"),
            "rejection_reason":     j.get("rejection_reason"),
            "suspension_reason":    j.get("suspension_reason"),
            "submitted_at":         j.get("submitted_at"),
            "reviewed_at":          j.get("reviewed_at"),
            "reviewed_by_admin_id": j.get("reviewed_by_admin_id"),
            "approved_at":          j.get("approved_at"),
            "suspended_at":         j.get("suspended_at"),
            "version":              j.get("version"),
            "metadata":             j.get("metadata") or {},
            "created_at":           j.get("created_at"),
            "updated_at":           j.get("updated_at"),
        }

    # ╔════════════════════════════════════════════════════════════════╗
    # ║ Listings (admin)                                               ║
    # ╚════════════════════════════════════════════════════════════════╝
    async def list_profiles(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        clauses, params = [], []
        if status:
            params.append(status)
            clauses.append(f"status = ${len(params)}::merchant_kyc_status")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])
        async with get_connection() as c:
            rows = await c.fetch(
                f"""
                SELECT * FROM merchant_kyc_profiles
                {where}
                ORDER BY COALESCE(submitted_at, updated_at) DESC
                LIMIT ${len(params) - 1} OFFSET ${len(params)}
                """,
                *params,
            )
        return [_profile_row(r) for r in rows]

    async def list_pending_reviews(self, *, limit: int = 50) -> list[dict]:
        async with get_connection() as c:
            rows = await c.fetch(
                """
                SELECT * FROM merchant_kyc_profiles
                WHERE status IN ('submitted', 'under_review')
                ORDER BY submitted_at NULLS LAST
                LIMIT $1
                """,
                limit,
            )
        return [_profile_row(r) for r in rows]

    async def list_audit_events(
        self,
        merchant_id: Optional[str | UUID] = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        if merchant_id is not None:
            async with get_connection() as c:
                rows = await c.fetch(
                    """
                    SELECT * FROM merchant_kyc_audit_events
                    WHERE merchant_id = $1::uuid
                    ORDER BY id DESC
                    LIMIT $2 OFFSET $3
                    """,
                    str(merchant_id), limit, offset,
                )
        else:
            async with get_connection() as c:
                rows = await c.fetch(
                    """
                    SELECT * FROM merchant_kyc_audit_events
                    ORDER BY id DESC
                    LIMIT $1 OFFSET $2
                    """,
                    limit, offset,
                )
        return [_audit_row(r) for r in rows]


kyc_service = KYCService()
