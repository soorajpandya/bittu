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
from app.core.audit_logger import audit_event
from app.services.razorpay import route as route_api
from app.services.razorpay.client import RazorpayBadRequestError

logger = get_logger(__name__)

# Razorpay surfaces a duplicate-email collision on POST /v2/accounts as
# `BAD_REQUEST_ERROR: Merchant email already exists for account - <id>`.
# When that happens we adopt the existing account rather than fail.
_DUPLICATE_EMAIL_RE = re.compile(
    r"already exists for account[^A-Za-z0-9]+([A-Za-z0-9]+)"
)

# Razorpay rejects `reference_id` when the merchant doesn't have the
# `route_code_support` / `account_code` feature flag. We retry the create
# call without `reference_id` in that case.
_REFERENCE_ID_FEATURE_RE = re.compile(
    r"(route\s*code\s*support|account_code\s*is\s*not\s*allowed)",
    re.IGNORECASE,
)

# Spec regexes (PAN/GST) per Razorpay create-linked-account docs.
_PAN_RE = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")
_GST_RE = re.compile(r"^[0-3][0-9][A-Z]{5}[0-9]{4}[A-Z][0-9][A-Z0-9]{2}$")

# Indian state normalisation.
#
# Razorpay's /v2/accounts API documents that it accepts either the
# 2-letter code (e.g. "GJ") OR the full uppercase name ("GUJARAT"),
# but in practice the codes are REJECTED with:
#   "State name entered is incorrect. Please provide correct state name."
# So we always emit the canonical UPPERCASE FULL NAME.
#
# Inputs we accept (case-insensitive):
#   • full name / common alternate spellings (e.g. "Tamil Nadu", "Tamilnadu")
#   • Razorpay's 2-letter codes (e.g. "GJ", "TG", "OR")
#   • ISO 3166-2:IN codes where they differ from Razorpay's (e.g. "TS"→Telangana,
#     "OD"→Odisha, "BR"→Bihar, "HR"→Haryana, "KL"→Kerala, "GA"→Goa,
#     "MN"→Manipur, "ML"→Meghalaya, "MZ"→Mizoram, "NL"→Nagaland, "PY"→Puducherry)
_STATE_CANONICAL: dict[str, str] = {
    # full names / spellings → canonical name
    "ANDAMAN & NICOBAR ISLANDS": "ANDAMAN AND NICOBAR ISLANDS",
    "ANDAMAN AND NICOBAR ISLANDS": "ANDAMAN AND NICOBAR ISLANDS",
    "ANDHRA PRADESH": "ANDHRA PRADESH",
    "ARUNACHAL PRADESH": "ARUNACHAL PRADESH",
    "ASSAM": "ASSAM",
    "BIHAR": "BIHAR",
    "CHANDIGARH": "CHANDIGARH",
    "CHHATTISGARH": "CHHATTISGARH",
    "DADRA & NAGAR HAVELI": "DADRA AND NAGAR HAVELI",
    "DADRA AND NAGAR HAVELI": "DADRA AND NAGAR HAVELI",
    "DAMAN & DIU": "DAMAN AND DIU",
    "DAMAN AND DIU": "DAMAN AND DIU",
    "DELHI": "DELHI",
    "GOA": "GOA",
    "GUJARAT": "GUJARAT",
    "HARYANA": "HARYANA",
    "HIMACHAL PRADESH": "HIMACHAL PRADESH",
    "JAMMU & KASHMIR": "JAMMU AND KASHMIR",
    "JAMMU AND KASHMIR": "JAMMU AND KASHMIR",
    "JHARKHAND": "JHARKHAND",
    "KARNATAKA": "KARNATAKA",
    "KERALA": "KERALA",
    "LAKSHADWEEP": "LAKSHADWEEP",
    "MADHYA PRADESH": "MADHYA PRADESH",
    "MAHARASHTRA": "MAHARASHTRA",
    "MANIPUR": "MANIPUR",
    "MEGHALAYA": "MEGHALAYA",
    "MIZORAM": "MIZORAM",
    "NAGALAND": "NAGALAND",
    "ODISHA": "ODISHA",
    "ORISSA": "ODISHA",
    "PONDICHERRY": "PUDUCHERRY",
    "PUDUCHERRY": "PUDUCHERRY",
    "PUNJAB": "PUNJAB",
    "RAJASTHAN": "RAJASTHAN",
    "SIKKIM": "SIKKIM",
    "TAMIL NADU": "TAMIL NADU",
    "TAMILNADU": "TAMIL NADU",
    "TELANGANA": "TELANGANA",
    "TRIPURA": "TRIPURA",
    "UTTAR PRADESH": "UTTAR PRADESH",
    "UTTARAKHAND": "UTTARAKHAND",
    "UTTARANCHAL": "UTTARAKHAND",
    "WEST BENGAL": "WEST BENGAL",
    # Razorpay 2-letter codes → canonical name
    "AN": "ANDAMAN AND NICOBAR ISLANDS", "AP": "ANDHRA PRADESH",
    "AR": "ARUNACHAL PRADESH", "AS": "ASSAM", "BI": "BIHAR",
    "CH": "CHANDIGARH", "CT": "CHHATTISGARH",
    "DN": "DADRA AND NAGAR HAVELI", "DD": "DAMAN AND DIU",
    "DL": "DELHI", "GO": "GOA", "GJ": "GUJARAT", "HA": "HARYANA",
    "HP": "HIMACHAL PRADESH", "JK": "JAMMU AND KASHMIR",
    "JH": "JHARKHAND", "KA": "KARNATAKA", "KE": "KERALA",
    "LD": "LAKSHADWEEP", "MP": "MADHYA PRADESH", "MH": "MAHARASHTRA",
    "MA": "MANIPUR", "ME": "MEGHALAYA", "MI": "MIZORAM",
    "NA": "NAGALAND", "OR": "ODISHA", "PO": "PUDUCHERRY",
    "PB": "PUNJAB", "RJ": "RAJASTHAN", "SK": "SIKKIM",
    "TN": "TAMIL NADU", "TG": "TELANGANA", "TR": "TRIPURA",
    "UP": "UTTAR PRADESH", "UT": "UTTARAKHAND", "WB": "WEST BENGAL",
    # ISO 3166-2:IN codes that diverge from Razorpay's codes
    "BR": "BIHAR", "GA": "GOA", "HR": "HARYANA", "KL": "KERALA",
    "MN": "MANIPUR", "ML": "MEGHALAYA", "MZ": "MIZORAM",
    "NL": "NAGALAND", "OD": "ODISHA", "PY": "PUDUCHERRY",
    "TS": "TELANGANA",
}


def _normalize_state(value: Any) -> Optional[str]:
    """Map a user-supplied state value to Razorpay's canonical
    uppercase full name (e.g. "GUJARAT", "TAMIL NADU").

    Returns ``None`` for empty input. For unknown inputs, returns the
    raw uppercased trimmed value so Razorpay surfaces a clear error
    rather than us silently dropping the field.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    up = s.upper()
    return _STATE_CANONICAL.get(up, up)


def _normalize_country(value: Any) -> Optional[str]:
    """Razorpay accepts a 2-letter uppercase ISO code (`IN`) or the
    lowercase full name (`india`). We canonicalise to the uppercase
    2-letter code when input is 2 chars, else lowercase the full name.
    Unknown inputs are passed through (trimmed) for Razorpay to validate.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if len(s) == 2:
        return s.upper()
    # Common-case shortcut so "India"/"INDIA"/"india" all work.
    if s.lower() == "india":
        return "IN"
    return s.lower()


_CONTACT_NAME_ALLOWED = re.compile(r"[^A-Za-z0-9 ]")


def _sanitize_contact_name(value: Any) -> Optional[str]:
    """Razorpay rejects names containing anything other than letters,
    digits and spaces (also rejects URLs/HTML/emails embedded in names).
    We strip disallowed characters and collapse whitespace. Returns
    ``None`` if the cleaned value is empty.
    """
    if value is None:
        return None
    cleaned = _CONTACT_NAME_ALLOWED.sub(" ", str(value))
    cleaned = " ".join(cleaned.split())
    return cleaned or None


def _normalize_phone(value: Any) -> Optional[str]:
    """Strip non-digits and a leading country prefix so we land in the
    8-15 char window Razorpay accepts."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return None
    # Drop a leading `91` for IN numbers when the remaining digits are a
    # plausible national number (>= 8 chars).
    if len(digits) > 10 and digits.startswith("91"):
        rest = digits[2:]
        if 8 <= len(rest) <= 15:
            digits = rest
    return digits


def _normalize_address(addr: Any) -> Optional[dict[str, Any]]:
    """Trim values, normalise state/country, ensure postal_code is a
    string of digits. Returns ``None`` for empty input.
    """
    if not addr or not isinstance(addr, Mapping):
        return None
    out: dict[str, Any] = {}
    for key in ("street1", "street2", "city"):
        v = addr.get(key)
        if v is None:
            continue
        sv = str(v).strip()
        if sv:
            out[key] = sv[:100]
    state = _normalize_state(addr.get("state"))
    if state:
        out["state"] = state
    pc = addr.get("postal_code")
    if pc is not None:
        pc_str = re.sub(r"\D", "", str(pc))
        if pc_str:
            out["postal_code"] = pc_str
    country = _normalize_country(addr.get("country"))
    if country:
        out["country"] = country
    return out or None


def _normalize_addresses_map(addresses: Any) -> dict[str, Any]:
    """Apply :func:`_normalize_address` to each address slot
    (`registered`, `operation`). Drops empty slots entirely."""
    if not addresses or not isinstance(addresses, Mapping):
        return {}
    out: dict[str, Any] = {}
    for slot in ("registered", "operation"):
        norm = _normalize_address(addresses.get(slot))
        if norm:
            out[slot] = norm
    return out


def _validate_pan(value: Any) -> Optional[str]:
    """Return upper-cased PAN if it matches the Razorpay regex, else
    ``None`` (so we omit ``legal_info.pan`` rather than 400-ing).
    """
    if value is None:
        return None
    s = str(value).strip().upper()
    return s if _PAN_RE.match(s) else None


def _validate_gst(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip().upper()
    return s if _GST_RE.match(s) else None


async def _safe_audit(**kwargs: Any) -> None:
    """Fire-and-forget audit wrapper — never raises out of business code."""
    try:
        await audit_event(**kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("rzp_route_audit_failed", action=kwargs.get("action"))


_ACCOUNT_STATES: tuple[str, ...] = (
    "created", "activated", "suspended", "rejected", "deleted",
)
_TRANSFER_STATES: tuple[str, ...] = (
    "created", "processed", "reversed", "failed",
)

# Razorpay rejects accounts in `needs_clarification` with a generic
# "contact support for Account Creation" banner in the hosted KYC flow
# when stakeholder.relationship is just `{executive: true}` for a company
# entity. For these business types Razorpay expects at least one director
# stakeholder. The map below picks the right default flags.
_COMPANY_BUSINESS_TYPES: frozenset[str] = frozenset({
    "private_limited", "public_limited", "llp",
})


def _default_relationship_for(business_type: Optional[str]) -> dict[str, bool]:
    """Return the default ``stakeholder.relationship`` flags Razorpay
    expects for a given business type. Companies need ``director: True``;
    everything else (proprietorship, partnership, individual, …) can use
    ``executive: True``."""
    bt = (business_type or "").strip().lower()
    if bt in _COMPANY_BUSINESS_TYPES:
        return {"director": True, "executive": True}
    return {"executive": True, "director": False}


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


def _derive_effective_status(d: dict) -> str:
    """Single value the FE can branch on without composing two fields.

    Maps the (account.status, route_product_status) pair into the user-
    facing onboarding state:

    - ``pending``             — no linked account on the merchant yet.
    - ``submitted``           — account exists, no route product requested yet.
    - ``under_review``        — product requested, awaiting Razorpay review.
    - ``needs_clarification`` — Razorpay wants more info on the product.
    - ``activated``           — product activated, settlements live.
    - ``suspended``           — account suspended on the gateway.
    - ``rejected``            — product activation rejected.

    The FE should branch off this and never re-derive from ``status`` /
    ``route_product_status`` directly.
    """
    if not d.get("linked_account_id"):
        return "pending"
    acc_status = (d.get("status") or "").lower()
    prod_status = (d.get("route_product_status") or "").lower()
    if acc_status == "suspended":
        return "suspended"
    if prod_status == "activated":
        return "activated"
    if prod_status == "rejected":
        return "rejected"
    if prod_status == "needs_clarification":
        return "needs_clarification"
    if prod_status in ("requested", "under_review", "created"):
        return "under_review"
    # Account exists but no product has been requested yet.
    return "submitted"


def _row_to_account(r) -> dict:
    if r is None:
        return {}
    d = dict(r)
    # NEVER expose bank_account_hash via API.
    d.pop("bank_account_hash", None)
    # FE convenience: a single onboarding state to branch UI off of.
    d["effective_status"] = _derive_effective_status(d)
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
                "SELECT legal_name, business_type, contact_email, contact_phone, "
                "       pan, gstin, registered_address "
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

    async def get_active_linked_account_id(
        self, merchant_id: Optional[str]
    ) -> Optional[str]:
        """Return the merchant's ``linked_account_id`` iff the Route product
        is activated and the account is not suspended.

        Used by the payment-intent flow to decide whether to attach a
        ``transfers[]`` split to the new order so funds auto-route to the
        merchant's linked account at capture time (instead of sitting on
        Bittu's master account until an out-of-band worker creates the
        transfer).
        """
        if not merchant_id:
            return None
        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                "SELECT linked_account_id, status::text AS status, "
                "       route_product_status "
                "FROM rzp_route_accounts WHERE merchant_id = $1::uuid",
                merchant_id,
            )
        if not row:
            return None
        linked_account_id = row["linked_account_id"]
        if not linked_account_id:
            return None
        if (row["status"] or "").lower() == "suspended":
            return None
        if (row["route_product_status"] or "").lower() != "activated":
            return None
        return linked_account_id

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
        category: Optional[str] = None,
        subcategory: Optional[str] = None,
        addresses_override: Optional[Mapping[str, Any]] = None,
        customer_facing_business_name: Optional[str] = None,
        contact_info: Optional[Mapping[str, Any]] = None,
        apps: Optional[Mapping[str, Any]] = None,
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

        raw_contact = beneficiary_name_override or owner.get("full_name") or profile.get("legal_name")
        contact_name = _sanitize_contact_name(raw_contact)
        if not contact_name or len(contact_name) < 4:
            raise ValueError(
                "contact_name must be 4..255 chars of letters/digits/spaces "
                "after sanitisation (got: %r)" % (raw_contact,)
            )
        contact_name = contact_name[:255]

        email = profile.get("contact_email") or owner.get("email")
        phone = _normalize_phone(profile.get("contact_phone") or owner.get("phone"))
        if not phone or not (8 <= len(phone) <= 15):
            raise ValueError("contact phone must be 8..15 digits after normalisation")

        notes = {"merchant_id": merchant_id}
        if extra_notes:
            notes.update(dict(extra_notes))

        # Build profile.addresses: prefer caller override, else the
        # KYC profile's registered_address (wrapped under the
        # `registered` key Razorpay expects).
        if addresses_override is not None:
            addresses = dict(addresses_override)
        else:
            reg_addr = profile.get("registered_address")
            if isinstance(reg_addr, str):
                try:
                    import json as _json
                    reg_addr = _json.loads(reg_addr)
                except Exception:
                    reg_addr = None
            addresses = {"registered": reg_addr} if reg_addr else {}

        addresses = _normalize_addresses_map(addresses)
        if not addresses.get("registered"):
            raise ValueError(
                "registered address required — send `addresses` "
                "(e.g. {\"registered\": {street1, city, state, postal_code, country}}) "
                "or fill `registered_address` on the KYC profile"
            )

        rzp_profile: dict[str, Any] = {
            "category": category or "food",
            "subcategory": subcategory or "restaurant",
            "addresses": addresses,
        }

        legal_info: dict[str, Any] = {}
        pan = _validate_pan(profile.get("pan"))
        if pan:
            legal_info["pan"] = pan
        elif profile.get("pan"):
            logger.warning(
                "rzp_route.pan_dropped merchant=%s reason=invalid_format value=%s",
                merchant_id, profile.get("pan"),
            )
        gst = _validate_gst(profile.get("gstin"))
        if gst:
            legal_info["gst"] = gst
        elif profile.get("gstin"):
            logger.warning(
                "rzp_route.gst_dropped merchant=%s reason=invalid_format value=%s",
                merchant_id, profile.get("gstin"),
            )

        # Razorpay's `reference_id` spec is internally inconsistent
        # (1..512 in the request schema, but errors complain about
        # 3..20 with `[A-Za-z0-9_-]`). Stay safely inside the strict
        # window. The default `m_<16 hex>` is 18 chars by construction.
        if reference_id:
            ref = re.sub(r"[^A-Za-z0-9_-]", "", str(reference_id))[:20] or None
            if ref and len(ref) < 3:
                ref = None
        else:
            ref = "m_" + merchant_id.replace("-", "")[:16]

        cfb_name = customer_facing_business_name
        if cfb_name is not None:
            cfb_name = str(cfb_name).strip()[:255] or None

        async def _post_create(*, with_ref: Optional[str]) -> dict:
            return await route_api.create_linked_account(
                email=email,
                phone=phone,
                legal_business_name=str(profile["legal_name"])[:200],
                business_type=str(profile.get("business_type") or "individual"),
                contact_name=contact_name,
                profile=rzp_profile,
                legal_info=legal_info or None,
                notes=notes,
                reference_id=with_ref,
                customer_facing_business_name=cfb_name,
                contact_info=dict(contact_info) if contact_info else None,
                apps=dict(apps) if apps else None,
                idempotency_key=f"rzp_route_account:{merchant_id}",
                merchant_id=merchant_id,
            )

        try:
            rzp_resp = await _post_create(with_ref=ref)
        except RazorpayBadRequestError as exc:
            desc = (exc.error_description or str(exc) or "")
            # 1. Account already exists for this email — adopt it.
            m = _DUPLICATE_EMAIL_RE.search(desc)
            if m:
                adopted_id = m.group(1)
                logger.warning(
                    "rzp_route.adopt_existing_account merchant=%s account=%s reason=duplicate_email",
                    merchant_id, adopted_id,
                )
                try:
                    rzp_resp = await route_api.fetch_linked_account(
                        adopted_id, merchant_id=merchant_id,
                    )
                except RazorpayBadRequestError as fetch_exc:
                    # The existing account belongs to a different Razorpay
                    # platform (different API key owner) — we can't adopt
                    # it. Surface a clear, actionable error instead of the
                    # opaque "Access Denied" from the fetch attempt.
                    fdesc = (fetch_exc.error_description or str(fetch_exc) or "").lower()
                    if "access denied" in fdesc or "does not exist" in fdesc:
                        logger.warning(
                            "rzp_route.adopt_failed merchant=%s account=%s reason=%s",
                            merchant_id, adopted_id, fdesc[:200],
                        )
                        raise ValueError(
                            f"The contact email '{email}' is already registered with a "
                            "Razorpay linked account that is not managed by this platform "
                            f"(account id: {adopted_id}). Please use a different contact "
                            "email for this merchant, or contact Razorpay support to release "
                            "the existing account."
                        ) from fetch_exc
                    raise
            # 2. Merchant doesn't have the route_code_support feature —
            # retry once without reference_id.
            elif ref and _REFERENCE_ID_FEATURE_RE.search(desc):
                logger.warning(
                    "rzp_route.retry_without_reference_id merchant=%s reason=%s",
                    merchant_id, desc[:200],
                )
                await _safe_audit(
                    domain="razorpay_route",
                    action="rzp_route.linked_account.reference_id_dropped",
                    entity_type="rzp_route_account",
                    entity_id=None,
                    payload={"merchant_id": merchant_id, "reason": desc[:200]},
                )
                rzp_resp = await _post_create(with_ref=None)
            else:
                raise

        # Persist via the single UPSERT path so the row binding lives in
        # exactly one place.
        await self.upsert_linked_account_from_razorpay(
            rzp_entity=rzp_resp, merchant_id_override=merchant_id,
        )
        await _safe_audit(
            domain="razorpay_route",
            action="rzp_route.linked_account.create",
            entity_type="rzp_route_account",
            entity_id=str(rzp_resp.get("id")) if rzp_resp.get("id") else None,
            payload={
                "merchant_id": merchant_id,
                "linked_account_id": rzp_resp.get("id"),
                "reference_id": reference_id,
            },
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

    async def heal_linked_account_from_kyc(
        self,
        *,
        merchant_id: str,
        category: Optional[str] = None,
        subcategory: Optional[str] = None,
        addresses_override: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        """PATCH an existing linked account with any ``legal_info`` /
        ``profile`` fields we now have in KYC but Razorpay doesn't.

        Razorpay returns ``needs_clarification`` on the Route product if
        e.g. ``legal_info.pan`` (company PAN) or ``legal_info.gst`` are
        missing for a registered entity. When the merchant submits the
        onboard form a second time with these now filled in, we PATCH
        the account so the new values reach Razorpay.

        No-op (just resync) if there's nothing new to send."""
        existing = await self._existing_account(merchant_id)
        if not existing or not existing["linked_account_id"]:
            raise LookupError("No linked account provisioned for this merchant")
        account_id = existing["linked_account_id"]

        # Pull the latest Razorpay view so we only PATCH genuinely missing
        # fields (Razorpay rejects PATCHes that set fields back to the
        # same value with the same correlation id occasionally).
        rzp_current = await route_api.fetch_linked_account(
            account_id, merchant_id=merchant_id,
        )
        snap = await self._kyc_snapshot(merchant_id)
        profile = snap["profile"]
        rzp_legal = (rzp_current.get("legal_info") or {}) if isinstance(
            rzp_current.get("legal_info"), dict
        ) else {}
        rzp_profile = (rzp_current.get("profile") or {}) if isinstance(
            rzp_current.get("profile"), dict
        ) else {}

        patch_body: dict[str, Any] = {}

        # legal_info: company PAN + GST.
        legal_patch: dict[str, Any] = {}
        if profile.get("pan") and not rzp_legal.get("pan"):
            legal_patch["pan"] = str(profile["pan"]).strip().upper()
        if profile.get("gstin") and not rzp_legal.get("gst"):
            legal_patch["gst"] = str(profile["gstin"]).strip().upper()
        if legal_patch:
            patch_body["legal_info"] = legal_patch

        # profile.addresses / category / subcategory updates.
        profile_patch: dict[str, Any] = {}
        if category and category != rzp_profile.get("category"):
            profile_patch["category"] = category
        if subcategory and subcategory != rzp_profile.get("subcategory"):
            profile_patch["subcategory"] = subcategory

        if addresses_override is not None:
            new_addresses = dict(addresses_override)
        else:
            reg_addr = profile.get("registered_address")
            if isinstance(reg_addr, str):
                try:
                    reg_addr = json.loads(reg_addr)
                except Exception:
                    reg_addr = None
            new_addresses = {"registered": reg_addr} if reg_addr else {}

        rzp_addresses = rzp_profile.get("addresses") or {}
        # Only send addresses if we have a non-empty value and Razorpay
        # doesn't already have a registered address.
        if (
            new_addresses
            and any(new_addresses.values())
            and not rzp_addresses.get("registered")
        ):
            profile_patch["addresses"] = new_addresses

        if profile_patch:
            patch_body["profile"] = profile_patch

        if not patch_body:
            # Nothing to send — just resync local state.
            await self.upsert_linked_account_from_razorpay(
                rzp_entity=rzp_current, merchant_id_override=merchant_id,
            )
            return await self.get_linked_account(merchant_id=merchant_id)

        try:
            rzp_resp = await route_api.update_linked_account(
                account_id, body=patch_body, merchant_id=merchant_id,
            )
        except RazorpayBadRequestError as exc:
            logger.warning(
                "rzp_route.account.patch_failed",
                merchant_id=merchant_id,
                account_id=account_id,
                error=str(exc),
                patch_body=patch_body,
            )
            # Don't fail the whole onboarding — fall back to resync.
            rzp_resp = await route_api.fetch_linked_account(
                account_id, merchant_id=merchant_id,
            )

        await self.upsert_linked_account_from_razorpay(
            rzp_entity=rzp_resp, merchant_id_override=merchant_id,
        )
        await _safe_audit(
            domain="razorpay_route",
            action="rzp_route.linked_account.update",
            entity_type="rzp_route_account",
            entity_id=account_id,
            payload={
                "merchant_id": merchant_id,
                "linked_account_id": account_id,
                "patched_keys": sorted(patch_body.keys()),
                "self_heal": True,
            },
        )
        return await self.get_linked_account(merchant_id=merchant_id)

    async def get_linked_account(self, *, merchant_id: str) -> dict:
        row = await self._existing_account(merchant_id)
        # Opportunistic refresh: while the merchant is in a non-terminal
        # onboarding state, the FE polls this endpoint every few seconds
        # waiting for ``activated``. The background polling scheduler only
        # runs every 12h, so without this the local mirror can stay stuck
        # at e.g. ``needs_clarification`` long after Razorpay has flipped
        # the product to ``activated``. Throttle to once every 8s per
        # merchant so we don't hammer Razorpay during tight polling.
        if row and row["linked_account_id"] and row["route_product_id"]:
            prod_status = (row["route_product_status"] or "").lower()
            if prod_status not in {"activated", "rejected"}:
                updated_at = row["updated_at"] if "updated_at" in row.keys() else None
                stale = True
                if updated_at is not None:
                    from datetime import datetime, timezone
                    age = (datetime.now(timezone.utc) - updated_at).total_seconds()
                    stale = age >= 8.0
                if stale:
                    try:
                        await self.sync_route_product(merchant_id=merchant_id)
                        row = await self._existing_account(merchant_id)
                    except Exception as exc:
                        logger.warning(
                            "rzp_route.get.product_refresh_failed",
                            merchant_id=merchant_id,
                            error=str(exc),
                        )
        return _row_to_account(row)

    async def fetch_linked_account_details(self, *, merchant_id: str) -> dict:
        """Mirror of Razorpay ``GET /v2/accounts/:account_id``.

        Returns the **full gateway payload** (id, type, status, email,
        phone, profile.{category,subcategory,addresses,business_model},
        legal_info, notes, contact_name, contact_info, apps, brand,
        business_type, legal_business_name, customer_facing_business_name,
        created_at, reference_id). Also re-syncs the local row so the
        cheap ``GET /linked-account`` stays consistent.

        Adds two convenience keys for the caller:
        ``merchant_id`` (string) and ``local_status`` (our enum).
        """
        existing = await self._existing_account(merchant_id)
        if not existing or not existing["linked_account_id"]:
            raise LookupError("No linked account provisioned for this merchant")
        account_id = existing["linked_account_id"]

        try:
            rzp_resp = await route_api.fetch_linked_account(
                account_id, merchant_id=merchant_id,
            )
        except RazorpayBadRequestError as exc:
            desc = exc.error_description or str(exc)
            # Razorpay returns 400 with "Linked account does not exist"
            # when the id is gone (deleted on dashboard, wrong env, …).
            # Surface as a 404 to the caller since the LookupError path
            # is the same UX.
            if "does not exist" in desc.lower():
                raise LookupError(desc)
            raise

        await self.upsert_linked_account_from_razorpay(
            rzp_entity=rzp_resp, merchant_id_override=merchant_id,
        )
        # Refresh local row to pick up the new status / razorpay_status
        # post-upsert before we annotate the response.
        refreshed = await self._existing_account(merchant_id)
        out = dict(rzp_resp)
        out["merchant_id"] = merchant_id
        if refreshed:
            row_dict = dict(refreshed)
            if "status" in row_dict:
                out["local_status"] = row_dict["status"]
            # Same single-value onboarding state exposed by GET /linked-account,
            # so callers of /details don't need to issue a second request.
            out["effective_status"] = _derive_effective_status(row_dict)
            if "route_product_status" in row_dict:
                out["route_product_status"] = row_dict["route_product_status"]
        return out

    # ── Merchant-driven update (PATCH /v2/accounts/:id) ─────────────────

    async def update_linked_account_details(
        self,
        *,
        merchant_id: str,
        phone: Optional[str] = None,
        legal_business_name: Optional[str] = None,
        customer_facing_business_name: Optional[str] = None,
        reference_id: Optional[str] = None,
        contact_name: Optional[str] = None,
        notes: Optional[Mapping[str, Any]] = None,
        category: Optional[str] = None,
        subcategory: Optional[str] = None,
        business_model: Optional[str] = None,
        addresses: Optional[Mapping[str, Any]] = None,
        pan: Optional[str] = None,
        gst: Optional[str] = None,
        contact_info: Optional[Mapping[str, Any]] = None,
        apps: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        """Apply a merchant-driven PATCH to the Razorpay linked account.

        Mirrors the Razorpay ``PATCH /v2/accounts/:account_id`` spec:
        all fields are optional, ``business_type`` and ``email`` cannot
        be updated and are not accepted here. Values are normalised
        (state/country/phone/contact_name) and invalid PAN/GST is
        silently dropped, consistent with ``provision_linked_account``.
        Returns the local row after upserting the gateway response.
        """
        existing = await self._existing_account(merchant_id)
        if not existing or not existing["linked_account_id"]:
            raise LookupError("No linked account provisioned for this merchant")
        account_id = existing["linked_account_id"]

        patch_body: dict[str, Any] = {}

        phone_norm = _normalize_phone(phone)
        if phone_norm:
            if not (8 <= len(phone_norm) <= 15):
                raise ValueError("phone must be 8..15 digits after normalisation")
            patch_body["phone"] = phone_norm

        if legal_business_name is not None:
            lbn = str(legal_business_name).strip()
            if lbn and not (4 <= len(lbn) <= 200):
                raise ValueError("legal_business_name must be 4..200 chars")
            if lbn:
                patch_body["legal_business_name"] = lbn

        if customer_facing_business_name is not None:
            cfbn = str(customer_facing_business_name).strip()
            if cfbn:
                patch_body["customer_facing_business_name"] = cfbn[:255]

        if reference_id is not None:
            ref_clean = re.sub(r"[^A-Za-z0-9_-]", "", str(reference_id))[:512]
            if ref_clean:
                patch_body["reference_id"] = ref_clean

        if contact_name is not None:
            cn = _sanitize_contact_name(contact_name)
            if cn and len(cn) < 4:
                raise ValueError("contact_name must be >= 4 chars after sanitisation")
            if cn:
                patch_body["contact_name"] = cn[:255]

        if notes:
            patch_body["notes"] = dict(notes)

        # profile.{category, subcategory, business_model, addresses}
        profile_patch: dict[str, Any] = {}
        if category:
            profile_patch["category"] = str(category).strip()
        if subcategory:
            profile_patch["subcategory"] = str(subcategory).strip()
        if business_model:
            bm = str(business_model).strip()
            if bm:
                profile_patch["business_model"] = bm[:255]
        norm_addresses = _normalize_addresses_map(addresses) if addresses else {}
        if norm_addresses:
            profile_patch["addresses"] = norm_addresses
        if profile_patch:
            patch_body["profile"] = profile_patch

        # legal_info.{pan, gst} — silently drop invalid values.
        legal_patch: dict[str, Any] = {}
        if pan is not None:
            pan_v = _validate_pan(pan)
            if pan_v:
                legal_patch["pan"] = pan_v
            else:
                logger.warning(
                    "rzp_route.pan_dropped",
                    merchant_id=merchant_id, account_id=account_id,
                )
        if gst is not None:
            gst_v = _validate_gst(gst)
            if gst_v:
                legal_patch["gst"] = gst_v
            else:
                logger.warning(
                    "rzp_route.gst_dropped",
                    merchant_id=merchant_id, account_id=account_id,
                )
        if legal_patch:
            patch_body["legal_info"] = legal_patch

        if contact_info:
            patch_body["contact_info"] = dict(contact_info)
        if apps:
            patch_body["apps"] = dict(apps)

        if not patch_body:
            return await self.get_linked_account(merchant_id=merchant_id)

        try:
            rzp_resp = await route_api.update_linked_account(
                account_id, body=patch_body, merchant_id=merchant_id,
            )
        except RazorpayBadRequestError as exc:
            desc = exc.error_description or str(exc)
            # Retry without reference_id if the platform lacks the feature.
            if "reference_id" in patch_body and _REFERENCE_ID_FEATURE_RE.search(desc):
                retry_body = {k: v for k, v in patch_body.items() if k != "reference_id"}
                await _safe_audit(
                    domain="razorpay_route",
                    action="rzp_route.linked_account.reference_id_dropped",
                    entity_type="rzp_route_account",
                    entity_id=account_id,
                    payload={"merchant_id": merchant_id, "reason": desc[:200]},
                )
                if not retry_body:
                    return await self.get_linked_account(merchant_id=merchant_id)
                rzp_resp = await route_api.update_linked_account(
                    account_id, body=retry_body, merchant_id=merchant_id,
                )
            else:
                raise

        await self.upsert_linked_account_from_razorpay(
            rzp_entity=rzp_resp, merchant_id_override=merchant_id,
        )
        await _safe_audit(
            domain="razorpay_route",
            action="rzp_route.linked_account.update",
            entity_type="rzp_route_account",
            entity_id=account_id,
            payload={
                "merchant_id": merchant_id,
                "linked_account_id": account_id,
                "patched_keys": sorted(patch_body.keys()),
            },
        )
        return await self.get_linked_account(merchant_id=merchant_id)

    # ── Stakeholder (Route onboarding step 3) ────────────────────────────

    async def create_stakeholder_for_merchant(
        self,
        *,
        merchant_id: str,
        relationship_overrides: Optional[Mapping[str, Any]] = None,
        kyc_overrides: Optional[Mapping[str, Any]] = None,
        addresses_overrides: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        """
        Step 3 of Route onboarding. Creates the stakeholder on Razorpay
        using the primary KYC owner row and persists the returned
        ``stakeholder_id`` on ``rzp_route_accounts``.

        Idempotent: if a stakeholder is already bound for this merchant we
        refetch + re-upsert instead of creating a second one.
        """
        existing = await self._existing_account(merchant_id)
        if not existing or not existing["linked_account_id"]:
            raise LookupError("No linked account provisioned for this merchant")

        account_id = existing["linked_account_id"]
        existing_stakeholder_id = existing["stakeholder_id"] if "stakeholder_id" in existing.keys() else None

        # Build the desired stakeholder body up front so we can use it
        # for both create and self-healing PATCH paths.
        snap = await self._kyc_snapshot(merchant_id)
        owner = snap["owner"]
        profile = snap["profile"]

        if not owner or not owner.get("full_name"):
            raise ValueError(
                "KYC owner missing — add a primary owner before creating a stakeholder"
            )

        # Razorpay v2 stakeholder `relationship` only accepts a fixed set
        # of boolean flags (e.g. director, executive). Sending an
        # `owner` key gets rejected with:
        #   "owner is/are not required and should not be sent"
        # For company entities (private_limited / public_limited / llp)
        # Razorpay requires at least one *director* — sending just
        # `executive: true` flips the product to `needs_clarification`
        # and the hosted KYC widget shows "contact support for Account
        # Creation". Pick the default by business_type.
        relationship = _default_relationship_for(profile.get("business_type"))
        if relationship_overrides:
            relationship.update(dict(relationship_overrides))
            relationship.pop("owner", None)

        # Razorpay v2 stakeholders require `phone` as an object (and the
        # SDK error surfaces it as "phone must be an array"). Strip any
        # `+91`/`+`/whitespace so we send the bare 10-digit primary.
        raw_phone = (owner.get("phone") or profile.get("contact_phone") or "").strip()
        digits = re.sub(r"\D", "", raw_phone)
        if digits.startswith("91") and len(digits) > 10:
            digits = digits[-10:]
        phone_obj = {"primary": digits} if digits else None

        # Auto-populate stakeholder.kyc.pan from the owner row when
        # present — Razorpay's KYC engine needs a PAN on the signatory
        # stakeholder before the product can leave `needs_clarification`.
        kyc_block: dict[str, Any] = {}
        if owner.get("pan"):
            kyc_block["pan"] = str(owner["pan"]).strip().upper()
        if kyc_overrides:
            kyc_block.update(dict(kyc_overrides))

        body: dict[str, Any] = {
            "name": owner.get("full_name"),
            "email": owner.get("email") or profile.get("contact_email"),
            "phone": phone_obj,
            "relationship": relationship,
        }
        if kyc_block:
            body["kyc"] = kyc_block
        if addresses_overrides:
            body["addresses"] = dict(addresses_overrides)

        if existing_stakeholder_id:
            # Self-heal path: PATCH the existing stakeholder so legacy
            # rows created before the director / kyc.pan defaults landed
            # get upgraded automatically on the next onboard retry.
            # PATCH only the keys Razorpay accepts on update.
            patch_body = {
                "name": body["name"],
                "email": body["email"],
                "phone": body["phone"],
                "relationship": body["relationship"],
            }
            if body.get("kyc"):
                patch_body["kyc"] = body["kyc"]
            if body.get("addresses"):
                patch_body["addresses"] = body["addresses"]
            try:
                rzp_resp = await route_api.update_stakeholder(
                    account_id, existing_stakeholder_id,
                    body=patch_body, merchant_id=merchant_id,
                )
            except RazorpayBadRequestError as exc:
                logger.warning(
                    "rzp_route.stakeholder.patch_failed_falling_back_to_fetch",
                    merchant_id=merchant_id,
                    stakeholder_id=existing_stakeholder_id,
                    error=str(exc),
                )
                rzp_resp = await route_api.fetch_stakeholder(
                    account_id, existing_stakeholder_id, merchant_id=merchant_id,
                )
            await self._persist_stakeholder(merchant_id, rzp_resp)
            await _safe_audit(
                domain="razorpay_route",
                action="rzp_route.stakeholder.update",
                entity_type="rzp_route_stakeholder",
                entity_id=existing_stakeholder_id,
                payload={
                    "merchant_id": merchant_id,
                    "linked_account_id": account_id,
                    "self_heal": True,
                },
            )
            return await self.get_linked_account(merchant_id=merchant_id)

        rzp_resp = await route_api.create_stakeholder(
            account_id,
            body=body,
            idempotency_key=f"rzp_route_stakeholder:{merchant_id}",
            merchant_id=merchant_id,
        )
        await self._persist_stakeholder(merchant_id, rzp_resp)
        await _safe_audit(
            domain="razorpay_route",
            action="rzp_route.stakeholder.create",
            entity_type="rzp_route_stakeholder",
            entity_id=str(rzp_resp.get("id")) if rzp_resp.get("id") else None,
            payload={
                "merchant_id": merchant_id,
                "linked_account_id": account_id,
            },
        )
        return await self.get_linked_account(merchant_id=merchant_id)

    async def _persist_stakeholder(
        self, merchant_id: str, rzp_entity: Mapping[str, Any]
    ) -> None:
        sth_id = rzp_entity.get("id")
        if not sth_id:
            return
        async with get_service_connection() as conn:
            await conn.execute(
                """
                UPDATE rzp_route_accounts
                   SET stakeholder_id  = $2,
                       stakeholder_raw = $3::jsonb,
                       updated_at      = NOW()
                 WHERE merchant_id = $1::uuid
                """,
                merchant_id, sth_id, json.dumps(dict(rzp_entity)),
            )

    # ── Product configuration (Route onboarding steps 4 & 5) ─────────────

    async def request_route_product(
        self,
        *,
        merchant_id: str,
        tnc_accepted: bool = True,
    ) -> dict:
        """
        Step 4 of Route onboarding. Requests the ``route`` product config.
        Idempotent: if a product is already bound we refetch + re-upsert.
        """
        existing = await self._existing_account(merchant_id)
        if not existing or not existing["linked_account_id"]:
            raise LookupError("No linked account provisioned for this merchant")
        if not existing["stakeholder_id"]:
            raise ValueError(
                "Stakeholder must be created before requesting a product configuration"
            )

        account_id = existing["linked_account_id"]
        existing_product_id = existing["route_product_id"]

        if existing_product_id:
            rzp_resp = await route_api.fetch_product_configuration(
                account_id, existing_product_id, merchant_id=merchant_id,
            )
            await self._persist_product(merchant_id, rzp_resp, tnc_accepted=tnc_accepted)
            return await self.get_linked_account(merchant_id=merchant_id)

        rzp_resp = await route_api.request_product_configuration(
            account_id,
            body={"product_name": "route", "tnc_accepted": bool(tnc_accepted)},
            idempotency_key=f"rzp_route_product:{merchant_id}",
            merchant_id=merchant_id,
        )
        await self._persist_product(
            merchant_id, rzp_resp, tnc_accepted=tnc_accepted, mark_requested=True,
        )
        return await self.get_linked_account(merchant_id=merchant_id)

    async def update_route_product_with_bank(
        self,
        *,
        merchant_id: str,
        bank_account_number: str,
        ifsc: str,
        beneficiary_name: Optional[str] = None,
        tnc_accepted: bool = True,
    ) -> dict:
        """
        Step 5 of Route onboarding. Sends settlement bank details to
        Razorpay so the product configuration can be activated. The full
        account number is used in-memory only — we persist last4 + sha256.
        """
        existing = await self._existing_account(merchant_id)
        if not existing or not existing["linked_account_id"]:
            raise LookupError("No linked account provisioned for this merchant")
        if not existing["route_product_id"]:
            raise ValueError(
                "Route product configuration must be requested before bank update"
            )

        if not bank_account_number or not ifsc:
            raise ValueError("bank_account_number and ifsc are required")

        snap = await self._kyc_snapshot(merchant_id)
        beneficiary = (
            beneficiary_name
            or snap["bank"].get("account_holder_name")
            or snap["owner"].get("full_name")
            or snap["profile"].get("legal_name")
        )
        if not beneficiary:
            raise ValueError("beneficiary_name required and not derivable from KYC")

        body: dict[str, Any] = {
            "settlements": {
                "account_number":   bank_account_number,
                "ifsc_code":        ifsc,
                "beneficiary_name": beneficiary,
            },
            "tnc_accepted": bool(tnc_accepted),
        }

        rzp_resp = await route_api.update_product_configuration(
            existing["linked_account_id"],
            existing["route_product_id"],
            body=body,
            merchant_id=merchant_id,
        )
        await self._persist_product(merchant_id, rzp_resp, tnc_accepted=tnc_accepted)
        await _safe_audit(
            domain="razorpay_route",
            action="rzp_route.product.bank_update",
            entity_type="rzp_route_product",
            entity_id=existing["route_product_id"],
            payload={
                "merchant_id": merchant_id,
                "linked_account_id": existing["linked_account_id"],
                "bank_account_last4": _last4(bank_account_number),
                "ifsc": ifsc,
                "product_status": (rzp_resp or {}).get("activation_status"),
            },
        )

        # Mirror bank fields locally (last4 + sha256 only — never the raw number).
        last4 = _last4(bank_account_number)
        bhash = _hash_account(bank_account_number)
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

    async def sync_route_product(self, *, merchant_id: str) -> dict:
        existing = await self._existing_account(merchant_id)
        if not existing or not existing["linked_account_id"]:
            raise LookupError("No linked account provisioned for this merchant")
        if not existing["route_product_id"]:
            raise LookupError("No route product configuration on this account")
        rzp_resp = await route_api.fetch_product_configuration(
            existing["linked_account_id"],
            existing["route_product_id"],
            merchant_id=merchant_id,
        )
        await self._persist_product(merchant_id, rzp_resp)
        return await self.get_linked_account(merchant_id=merchant_id)

    async def _persist_product(
        self,
        merchant_id: str,
        rzp_entity: Mapping[str, Any],
        *,
        tnc_accepted: Optional[bool] = None,
        mark_requested: bool = False,
    ) -> None:
        product_id = rzp_entity.get("id")
        if not product_id:
            return
        status = (rzp_entity.get("activation_status") or rzp_entity.get("status") or "").lower() or None
        async with get_service_connection() as conn:
            await conn.execute(
                """
                UPDATE rzp_route_accounts
                   SET route_product_id            = $2,
                       route_product_status        = COALESCE($3, route_product_status),
                       route_product_raw           = $4::jsonb,
                       route_product_requested_at  = CASE
                           WHEN $5::boolean AND route_product_requested_at IS NULL THEN NOW()
                           ELSE route_product_requested_at END,
                       route_product_activated_at  = CASE
                           WHEN $3 = 'activated' AND route_product_activated_at IS NULL THEN NOW()
                           ELSE route_product_activated_at END,
                       tnc_accepted_at             = CASE
                           WHEN $6::boolean AND tnc_accepted_at IS NULL THEN NOW()
                           ELSE tnc_accepted_at END,
                       updated_at                  = NOW()
                 WHERE merchant_id = $1::uuid
                """,
                merchant_id,
                product_id,
                status,
                json.dumps(dict(rzp_entity)),
                bool(mark_requested),
                bool(tnc_accepted) if tnc_accepted is not None else False,
            )

    # ── Full onboarding orchestrator ─────────────────────────────────────

    async def onboard_route_merchant(
        self,
        *,
        merchant_id: str,
        bank_account_number: str,
        ifsc: Optional[str] = None,
        beneficiary_name: Optional[str] = None,
        reference_id: Optional[str] = None,
        tnc_accepted: bool = True,
        extra_notes: Optional[Mapping[str, Any]] = None,
        category: Optional[str] = None,
        subcategory: Optional[str] = None,
        addresses_override: Optional[Mapping[str, Any]] = None,
        customer_facing_business_name: Optional[str] = None,
        contact_info: Optional[Mapping[str, Any]] = None,
        apps: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        """
        End-to-end Route onboarding orchestrator (steps 2-5 of the
        corrected flow). Each step is independently idempotent so callers
        can re-invoke on failure without producing duplicates.
        """
        # Step 2 — linked account (idempotent).
        await self.provision_linked_account(
            merchant_id=merchant_id,
            bank_account_number=None,  # bank goes to /products, not /accounts
            ifsc_override=ifsc,
            beneficiary_name_override=beneficiary_name,
            reference_id=reference_id,
            extra_notes=extra_notes,
            category=category,
            subcategory=subcategory,
            addresses_override=addresses_override,
            customer_facing_business_name=customer_facing_business_name,
            contact_info=contact_info,
            apps=apps,
        )
        # Step 2.5 — self-heal the linked account: if the caller filled
        # in PAN/GSTIN/addresses on this retry but the account was
        # created earlier without them, PATCH Razorpay so the product
        # can leave `needs_clarification`.
        await self.heal_linked_account_from_kyc(
            merchant_id=merchant_id,
            category=category,
            subcategory=subcategory,
            addresses_override=addresses_override,
        )
        # Step 3 — stakeholder (idempotent; PATCHes if one already exists).
        await self.create_stakeholder_for_merchant(merchant_id=merchant_id)
        # Step 4 — request product configuration (idempotent).
        await self.request_route_product(
            merchant_id=merchant_id, tnc_accepted=tnc_accepted,
        )
        # Step 5 — update product config with settlement bank details.
        snap_ifsc = ifsc
        if not snap_ifsc:
            snap = await self._kyc_snapshot(merchant_id)
            snap_ifsc = snap["bank"].get("ifsc")
        if not snap_ifsc:
            raise ValueError("ifsc required and not present in KYC primary bank")
        return await self.update_route_product_with_bank(
            merchant_id=merchant_id,
            bank_account_number=bank_account_number,
            ifsc=snap_ifsc,
            beneficiary_name=beneficiary_name,
            tnc_accepted=tnc_accepted,
        )

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

    async def _resolve_payment_context(
        self, razorpay_payment_id: Optional[str]
    ) -> dict:
        """Resolve `restaurant_id` (== merchant_id), `internal_order_id`,
        `razorpay_order_id` for a Razorpay payment id. Returns {} if unknown."""
        if not razorpay_payment_id:
            return {}
        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                "SELECT merchant_id::text AS restaurant_id, "
                "       internal_order_id::text AS internal_order_id, "
                "       razorpay_order_id "
                "FROM rzp_payments WHERE razorpay_payment_id = $1 "
                "ORDER BY created_at DESC LIMIT 1",
                razorpay_payment_id,
            )
        return dict(row) if row else {}

    # ── Settlement-ready gate ────────────────────────────────────────────

    async def assert_settlement_ready(self, *, merchant_id: Optional[str]) -> None:
        """Block payment intake when the merchant has opted into Route but
        has not finished onboarding (linked account not activated OR product
        configuration not activated).

        Merchants without ANY rzp_route_accounts row are treated as legacy
        (pass through) — a warning is logged so ops can chase migration.
        Raise ``PermissionError`` to be mapped to HTTP 409 at the boundary.
        """
        if not merchant_id:
            return
        row = await self._existing_account(merchant_id)
        if not row:
            logger.warning(
                "rzp_route_legacy_merchant_no_account",
                merchant_id=merchant_id,
            )
            return
        status = row["status"]
        product_status = row["route_product_status"] if "route_product_status" in row.keys() else None
        # Route linked accounts stay status='created' for their entire
        # happy-path lifetime; activation flows through the *product*. So
        # the gate is: product must be activated, and the account must
        # not be in a terminal-negative gateway state.
        if (status or "").lower() in {"suspended", "rejected"} or (product_status or "").lower() != "activated":
            raise PermissionError(
                "merchant_not_settlement_ready: linked_account_status="
                f"{status!r} product_status={product_status!r}"
            )

    async def upsert_transfer_from_razorpay(
        self,
        *,
        rzp_entity: Mapping[str, Any],
        merchant_id_override: Optional[str] = None,
        status_override: Optional[str] = None,
        refund_id_override: Optional[str] = None,
        reversal_of_transfer_id_override: Optional[str] = None,
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

        # Denormalised links (Phase 8). Razorpay attaches `recipient_settlement_id`
        # to the transfer entity once it has been included in a settlement batch;
        # we capture order/restaurant context from rzp_payments on first insert
        # so dashboards don't need a 4-table join.
        razorpay_payment_id = rzp_entity.get("source") or rzp_entity.get("razorpay_payment_id") or ""
        recipient_settlement_id = (
            rzp_entity.get("recipient_settlement_id")
            or rzp_entity.get("settlement_id")
        )
        ctx = await self._resolve_payment_context(razorpay_payment_id) if razorpay_payment_id else {}
        restaurant_id = ctx.get("restaurant_id")
        internal_order_id = ctx.get("internal_order_id")
        razorpay_order_id = ctx.get("razorpay_order_id")

        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO rzp_route_transfers (
                    transfer_id, razorpay_payment_id,
                    source_account_id, recipient_account_id,
                    merchant_id, amount_paise, currency,
                    on_hold, on_hold_until,
                    fee_paise, tax_paise, status,
                    notes, raw_payload, processed_at, reversed_at,
                    restaurant_id, internal_order_id, razorpay_order_id,
                    recipient_settlement_id, refund_id, reversal_of_transfer_id
                ) VALUES (
                    $1, $2, $3, $4, $5::uuid, $6, $7,
                    $8,
                    CASE WHEN $9::bigint IS NULL THEN NULL ELSE to_timestamp($9::bigint) END,
                    $10, $11, $12::rzp_route_transfer_state,
                    COALESCE($13::jsonb, '{}'::jsonb),
                    $14::jsonb,
                    CASE WHEN $15::bigint IS NULL THEN NULL ELSE to_timestamp($15::bigint) END,
                    CASE WHEN $16::bigint IS NULL THEN NULL ELSE to_timestamp($16::bigint) END,
                    $17::uuid, $18::uuid, $19, $20, $21, $22
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
                    restaurant_id            = COALESCE(rzp_route_transfers.restaurant_id, EXCLUDED.restaurant_id),
                    internal_order_id        = COALESCE(rzp_route_transfers.internal_order_id, EXCLUDED.internal_order_id),
                    razorpay_order_id        = COALESCE(rzp_route_transfers.razorpay_order_id, EXCLUDED.razorpay_order_id),
                    recipient_settlement_id  = COALESCE(EXCLUDED.recipient_settlement_id, rzp_route_transfers.recipient_settlement_id),
                    refund_id                = COALESCE(EXCLUDED.refund_id, rzp_route_transfers.refund_id),
                    reversal_of_transfer_id  = COALESCE(EXCLUDED.reversal_of_transfer_id, rzp_route_transfers.reversal_of_transfer_id),
                    updated_at           = NOW()
                RETURNING *
                """,
                transfer_id,
                razorpay_payment_id,
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
                restaurant_id,
                internal_order_id,
                razorpay_order_id,
                recipient_settlement_id,
                refund_id_override,
                reversal_of_transfer_id_override,
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
        await _safe_audit(
            domain="razorpay_route",
            action="rzp_route.transfer.create",
            entity_type="rzp_route_transfer",
            entity_id=(upserted[0].get("transfer_id") if upserted else None),
            payload={
                "merchant_id": merchant_id,
                "razorpay_payment_id": razorpay_payment_id,
                "amount_paise": int(amount_paise),
                "on_hold": bool(on_hold),
                "transfer_count": len(upserted),
            },
        )
        return {"transfers": upserted, "raw": rzp_resp}

    async def reverse_transfer(
        self,
        *,
        merchant_id: str,
        transfer_id: str,
        amount_paise: Optional[int] = None,
        notes: Optional[Mapping[str, Any]] = None,
        refund_id: Optional[str] = None,
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
        merged_notes: dict[str, Any] = dict(notes or {})
        if refund_id:
            merged_notes.setdefault("refund_id", refund_id)
        rzp_resp = await route_api.reverse_transfer(
            transfer_id,
            amount_paise=amount_paise,
            notes=merged_notes,
            idempotency_key=idem,
            merchant_id=merchant_id,
        )
        # Refetch the transfer to capture the new state.
        try:
            updated = await route_api.fetch_transfer(transfer_id, merchant_id=merchant_id)
            await self.upsert_transfer_from_razorpay(
                rzp_entity=updated,
                merchant_id_override=merchant_id,
                refund_id_override=refund_id,
                reversal_of_transfer_id_override=transfer_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("rzp_transfer_refetch_failed", transfer_id=transfer_id)
        await _safe_audit(
            domain="razorpay_route",
            action="rzp_route.transfer.reverse",
            entity_type="rzp_route_transfer",
            entity_id=transfer_id,
            payload={
                "merchant_id": merchant_id,
                "amount_paise": int(amount_paise) if amount_paise else None,
                "refund_id": refund_id,
            },
        )
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
