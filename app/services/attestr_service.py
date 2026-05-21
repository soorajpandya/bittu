"""
Attestr verification service.

Currently exposes:
  - verify_fssai_license(reg, fetch_products=False)

Auth: `Authorization: Basic <ATTESTR_AUTH_TOKEN>` where the token is the
already-base64-encoded `client_id:client_secret` pair (pulled verbatim from
the Attestr dashboard and stored in the environment). We do NOT re-encode.

Important Attestr quirk:
  The endpoint returns HTTP 200 even when the license is invalid; the
  response body carries `valid: false` plus a human-readable `message`.
  Callers must inspect the returned dict's `valid` field — we do not raise
  on `valid=false`.
"""
from __future__ import annotations

import re
from typing import Any, Optional, TypedDict

import httpx

from app.core.config import get_settings
from app.core.exceptions import AppException, RateLimitError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

_FSSAI_RE = re.compile(r"^\d{14}$")
_FSSAI_PATH = "/api/v1/public/checkx/fssai"


class FssaiProduct(TypedDict, total=False):
    """One product entry from `products[]` when `fetch_products=True`."""
    name: Optional[str]
    role: Optional[str]
    nature_of_business: Optional[str]
    active: Optional[bool]
    type: Optional[str]


class FssaiVerificationResult(TypedDict, total=False):
    """Shape of the dict returned by `verify_fssai_license`.

    Top-level keys mirror the Attestr response, except `nature_of_business`
    (snake_cased from Attestr's `natureOfBusiness`). When `valid=False` every
    optional field is `None`. `raw` always holds the full Attestr response
    for forward-compat / debugging.
    """
    valid: bool
    message: Optional[str]
    reg: Optional[str]
    status: Optional[str]           # e.g. "License Issued"
    active: Optional[bool]
    entity: Optional[str]
    uuid: Optional[int]
    category: Optional[str]         # e.g. "Central License"
    state: Optional[str]
    address: Optional[str]
    zip: Optional[int]
    products: Optional[list[FssaiProduct]]
    raw: dict[str, Any]


class AttestrConfigError(AppException):
    """Raised when the ATTESTR_AUTH_TOKEN is missing — operator misconfig."""
    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            detail="Attestr verification is not configured on this server.",
            error_code="ATTESTR_NOT_CONFIGURED",
            retryable=False,
        )


class AttestrUpstreamError(AppException):
    """Generic 502 surfaced when Attestr itself fails (5xx, auth, network)."""
    def __init__(self, detail: str, *, retryable: bool = False) -> None:
        super().__init__(
            status_code=502,
            detail=detail,
            error_code="ATTESTR_UPSTREAM_ERROR",
            retryable=retryable,
        )


def _auth_header() -> dict[str, str]:
    token = (get_settings().ATTESTR_AUTH_TOKEN or "").strip()
    if not token:
        raise AttestrConfigError()
    return {"Authorization": f"Basic {token}"}


def _extract_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            v = payload.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return fallback


def _normalize_products(raw_products: Any) -> Optional[list[FssaiProduct]]:
    if not isinstance(raw_products, list):
        return None
    out: list[FssaiProduct] = []
    for p in raw_products:
        if not isinstance(p, dict):
            continue
        out.append({
            "name":               p.get("name"),
            "role":               p.get("role"),
            "nature_of_business": p.get("natureOfBusiness"),
            "active":             p.get("active"),
            "type":               p.get("type"),
        })
    return out


def _normalize_fssai(
    payload: dict[str, Any],
    *,
    valid: bool,
    message: Optional[str],
) -> FssaiVerificationResult:
    return {
        "valid":    valid,
        "message":  message,
        "reg":      payload.get("reg"),
        "status":   payload.get("status"),
        "active":   payload.get("active"),
        "entity":   payload.get("entity"),
        "uuid":     payload.get("uuid"),
        "category": payload.get("category"),
        "state":    payload.get("state"),
        "address":  payload.get("address"),
        "zip":      payload.get("zip"),
        "products": _normalize_products(payload.get("products")),
        "raw":      payload,
    }


async def verify_fssai_license(
    reg: str,
    fetch_products: bool = False,
) -> FssaiVerificationResult:
    """
    Verify a 14-digit FSSAI license number via Attestr.

    Args:
        reg: The 14-digit FSSAI license number.
        fetch_products: If True, ask Attestr to also return the registered
            products list (slower, costs more credits).

    Returns:
        A dict shaped like `FssaiVerificationResult`. When `valid=True`,
        top-level fields (`entity`, `reg`, `status`, `address`, `state`,
        `category`, `zip`, `products`, `active`) carry Attestr's data.
        When `valid=False`, only `message` is populated and other fields
        are `None`. `raw` always holds the full Attestr payload.

        Example (valid):
            {
              "valid": True, "message": None,
              "reg": "10013012000249", "status": "License Issued",
              "entity": "Patanjali Ayurved Ltd.", "active": True,
              "category": "Central License", "state": "Uttarakhand",
              "address": "Unit-II, ...", "zip": 249402, "uuid": 66751215,
              "products": [{ "name": "...", "role": "...",
                             "nature_of_business": "...",
                             "active": True, "type": None }],
              "raw": { ...full Attestr response... }
            }

        Example (invalid):
            {"valid": False, "message": "No matching record found",
             "reg": None, "status": None, ..., "raw": {...}}

    Raises:
        ValidationError:     `reg` is not a 14-digit numeric string.
        RateLimitError:      Attestr returned 429.
        AttestrConfigError:  ATTESTR_AUTH_TOKEN env var is not set (503).
        AttestrUpstreamError:
            - 400 from Attestr (malformed / low credit) → non-retryable 502.
            - 401/403 from Attestr (our creds wrong)    → non-retryable 502.
            - 5xx, timeout, network failure             → retryable 502.
    """
    # ── Input validation ──
    if not isinstance(reg, str):
        raise ValidationError("FSSAI registration number must be a string.")
    reg = reg.strip()
    if not reg:
        raise ValidationError("FSSAI registration number is required.")
    if not _FSSAI_RE.match(reg):
        raise ValidationError(
            "FSSAI registration number must be exactly 14 digits."
        )

    settings = get_settings()
    url = f"{settings.ATTESTR_BASE_URL.rstrip('/')}{_FSSAI_PATH}"
    headers = {"Content-Type": "application/json", **_auth_header()}
    body: dict[str, Any] = {"reg": reg}
    if fetch_products:
        body["fetchProducts"] = True

    # ── Network call ──
    try:
        async with httpx.AsyncClient(timeout=settings.ATTESTR_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.TimeoutException:
        logger.warning("attestr_fssai_timeout", reg=reg)
        raise AttestrUpstreamError(
            "Attestr verification timed out. Please retry.", retryable=True
        )
    except httpx.HTTPError as exc:
        logger.warning("attestr_fssai_network_error", reg=reg, error=str(exc))
        raise AttestrUpstreamError(
            "Could not reach Attestr verification service.", retryable=True
        )

    # ── Status mapping ──
    status = resp.status_code
    # Be defensive — Attestr always returns JSON, but a gateway 5xx may not.
    try:
        payload = resp.json()
    except ValueError:
        payload = None

    if status == 200:
        if not isinstance(payload, dict):
            logger.error("attestr_fssai_bad_payload", reg=reg, body=resp.text[:500])
            raise AttestrUpstreamError("Attestr returned an unreadable response.")
        valid = bool(payload.get("valid"))
        message = _extract_message(payload, "") or None
        logger.info(
            "attestr_fssai_verified",
            reg=reg,
            valid=valid,
            fetch_products=fetch_products,
        )
        return _normalize_fssai(payload, valid=valid, message=message)

    if status == 400:
        msg = _extract_message(payload, "Attestr rejected the request (400).")
        logger.warning("attestr_fssai_bad_request", reg=reg, message=msg)
        raise AttestrUpstreamError(f"Attestr: {msg}", retryable=False)

    if status in (401, 403):
        logger.error("attestr_fssai_auth_failure", reg=reg, status=status)
        raise AttestrUpstreamError(
            "Attestr authentication failed. Verify ATTESTR_AUTH_TOKEN.",
            retryable=False,
        )

    if status == 429:
        logger.warning("attestr_fssai_rate_limited", reg=reg)
        raise RateLimitError()

    if 500 <= status < 600:
        msg = _extract_message(payload, f"Attestr server error ({status}).")
        logger.error("attestr_fssai_upstream_5xx", reg=reg, status=status, message=msg)
        raise AttestrUpstreamError(msg, retryable=True)

    # Anything else (e.g., 3xx, 4xx not enumerated above)
    msg = _extract_message(payload, f"Attestr returned unexpected status {status}.")
    logger.error("attestr_fssai_unexpected_status", reg=reg, status=status, message=msg)
    raise AttestrUpstreamError(msg, retryable=False)
