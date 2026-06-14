"""
Sub-Merchant Agreement acceptance — server-side audit trail.

The server is the source of truth. Trust-critical fields (`accepted_at`,
`ip`, `user_id`, `restaurant_id`, identity snapshot) are stamped server-side;
client-reported values are stored separately for comparison only.

Endpoints (all JWT-gated, merchant owner/manager):
    POST /api/v1/merchant/agreement-acceptance         — record an acceptance
    GET  /api/v1/merchant/agreement-acceptance/latest  — newest acceptance (proof)
    GET  /api/v1/merchant/agreement-acceptance         — full history (audit)
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.core.auth import UserContext, get_current_user
from app.core.config import get_settings
from app.core.database import get_connection
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.logging import get_logger

router = APIRouter(prefix="/merchant", tags=["Merchant Agreement"])
logger = get_logger(__name__)

# Agreement versions the server will accept. Bump when the legal text changes;
# the FE-computed plain text / sha256 must correspond to one of these.
KNOWN_AGREEMENT_VERSIONS: set[str] = {"1.0"}

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


# ── Auth helper ──────────────────────────────────────────────────────────


def require_merchant(user: UserContext = Depends(get_current_user)) -> UserContext:
    """Only the merchant principal (owner/manager) may accept the agreement."""
    if user.role not in ("owner", "manager"):
        raise ForbiddenError(
            f"Role '{user.role}' cannot accept the sub-merchant agreement. "
            "Required: owner or manager."
        )
    return user


def _parse_client_ts(value: Optional[str]) -> Optional[datetime]:
    """Best-effort parse of the client-reported ISO-8601 timestamp.

    asyncpg binds the value to a ``timestamptz`` parameter and requires a real
    ``datetime`` (it will not parse strings). This is non-authoritative
    comparison data, so an unparseable value is stored as NULL rather than
    failing the whole request.
    """
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    # Accept a trailing 'Z' (UTC) which datetime.fromisoformat rejects on <3.11.
    if raw.endswith("Z") or raw.endswith("z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _client_ip(request: Request) -> Optional[str]:
    """Real client IP, honouring X-Forwarded-For only from trusted proxies."""
    settings = get_settings()
    direct_ip = request.client.host if request.client else None
    if direct_ip and direct_ip in settings.TRUSTED_PROXIES:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or direct_ip
    return direct_ip


# ── Schemas ──────────────────────────────────────────────────────────────


class AgreementAcceptanceIn(BaseModel):
    version: str = Field(..., min_length=1, max_length=32)
    agreement_sha256: str = Field(..., min_length=64, max_length=64)
    accepted_at_client: Optional[str] = None  # client ISO-8601 timestamp
    user_agent: Optional[str] = Field(default=None, max_length=1024)
    ip_client: Optional[str] = Field(default=None, max_length=64)


class AgreementAcceptanceOut(BaseModel):
    id: str
    agreement_type: str
    version: str
    accepted_at: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    agreement_sha256: str
    user_id: str
    restaurant_id: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    business_name: Optional[str] = None
    accepted_at_client: Optional[str] = None
    ip_client: Optional[str] = None


# ── Internal helpers ───────────────────────────────────────────────────────


def _row_to_out(row) -> dict:
    d = dict(row)
    return {
        "id": str(d["id"]),
        "agreement_type": d.get("agreement_type"),
        "version": d.get("version"),
        "accepted_at": d["accepted_at"].isoformat() if d.get("accepted_at") else None,
        "ip": str(d["ip"]) if d.get("ip") is not None else None,
        "user_agent": d.get("user_agent"),
        "agreement_sha256": d.get("agreement_sha256"),
        "user_id": str(d["user_id"]) if d.get("user_id") else None,
        "restaurant_id": str(d["restaurant_id"]) if d.get("restaurant_id") else None,
        "name": d.get("name"),
        "email": d.get("email"),
        "business_name": d.get("business_name"),
        "accepted_at_client": (
            d["accepted_at_client"].isoformat() if d.get("accepted_at_client") else None
        ),
        "ip_client": d.get("ip_client"),
    }


async def _resolve_merchant_snapshot(conn, user: UserContext) -> dict:
    """Resolve the authoritative identity snapshot from the merchant record.

    Prefers the Razorpay linked-account legal/contact details (KYC source of
    truth) and falls back to the restaurant row. Never trusts the request body.
    """
    owner = user.owner_id or user.user_id
    rid = user.restaurant_id

    row = await conn.fetchrow(
        """
        SELECT r.id            AS restaurant_id,
               r.name          AS restaurant_name,
               r.email         AS restaurant_email,
               la.legal_business_name,
               la.contact_name,
               la.email        AS la_email
        FROM restaurants r
        LEFT JOIN rzp_route_accounts la ON la.merchant_id = r.id
        WHERE ($1::uuid IS NOT NULL AND r.id = $1::uuid)
           OR r.owner_id = $2
        ORDER BY ($1::uuid IS NOT NULL AND r.id = $1::uuid) DESC, r.created_at ASC
        LIMIT 1
        """,
        rid,
        str(owner),
    )

    if not row:
        return {
            "restaurant_id": rid,
            "name": None,
            "email": user.email,
            "business_name": None,
        }

    return {
        "restaurant_id": str(row["restaurant_id"]),
        "name": row["contact_name"] or row["restaurant_name"],
        "email": user.email or row["la_email"] or row["restaurant_email"],
        "business_name": row["legal_business_name"] or row["restaurant_name"],
    }


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post("/agreement-acceptance", status_code=201, response_model=AgreementAcceptanceOut)
async def record_agreement_acceptance(
    body: AgreementAcceptanceIn,
    request: Request,
    user: UserContext = Depends(require_merchant),
):
    """Persist an immutable acceptance record (append-only).

    The server stamps `accepted_at`, `ip`, identity and tenant fields. The
    client values for those are ignored (kept only in the *_client columns).
    """
    version = body.version.strip()
    if version not in KNOWN_AGREEMENT_VERSIONS:
        raise ValidationError(
            f"Unknown agreement version '{version}'. "
            f"Supported: {', '.join(sorted(KNOWN_AGREEMENT_VERSIONS))}."
        )

    sha = body.agreement_sha256.strip().lower()
    if not _SHA256_RE.match(sha):
        raise ValidationError("agreement_sha256 must be a 64-character hex string.")

    user_agent = body.user_agent or request.headers.get("user-agent", "")[:1024] or None
    server_ip = _client_ip(request)
    accepted_at_client = _parse_client_ts(body.accepted_at_client)

    async with get_connection() as conn:
        snap = await _resolve_merchant_snapshot(conn, user)
        row = await conn.fetchrow(
            """
            INSERT INTO merchant_agreement_acceptances (
                user_id, restaurant_id, agreement_type, version, agreement_sha256,
                ip, user_agent, name, email, business_name,
                accepted_at_client, ip_client
            ) VALUES (
                $1::uuid, $2::uuid, 'sub_merchant', $3, $4,
                $5::inet, $6, $7, $8, $9,
                $10::timestamptz, $11
            )
            RETURNING *
            """,
            str(user.user_id),
            snap["restaurant_id"],
            version,
            sha,
            server_ip,
            user_agent,
            snap["name"],
            snap["email"],
            snap["business_name"],
            accepted_at_client,
            body.ip_client,
        )

    logger.info(
        "merchant_agreement_accepted",
        user_id=str(user.user_id),
        restaurant_id=snap["restaurant_id"],
        version=version,
    )
    return _row_to_out(row)


@router.get("/agreement-acceptance/latest", response_model=AgreementAcceptanceOut)
async def get_latest_agreement_acceptance(
    user: UserContext = Depends(require_merchant),
):
    """Return the most recent acceptance for this merchant, or 404 if none."""
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM merchant_agreement_acceptances
            WHERE user_id = $1::uuid
            ORDER BY accepted_at DESC
            LIMIT 1
            """,
            str(user.user_id),
        )
    if not row:
        raise NotFoundError("AgreementAcceptance")
    return _row_to_out(row)


@router.get("/agreement-acceptance", response_model=list[AgreementAcceptanceOut])
async def list_agreement_acceptances(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_merchant),
):
    """Full acceptance history for this merchant (newest first) — audit view."""
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM merchant_agreement_acceptances
            WHERE user_id = $1::uuid
            ORDER BY accepted_at DESC
            LIMIT $2 OFFSET $3
            """,
            str(user.user_id),
            limit,
            offset,
        )
    return [_row_to_out(r) for r in rows]
