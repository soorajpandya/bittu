"""
RequestSecurityMiddleware — HMAC-SHA256 request signature verification.

Defends against on-the-wire tampering, replayed captures, and any client that
strips/forges the auth header chain. Pairs with `app.services.session_key_service`
(which issues + rotates the per-(user,device) signing secret) and the Flutter
`_RequestSecurityHeadersInterceptor` that produces the headers.

Required request headers
------------------------
    X-Device-Id   stable per-install client id (UUID/string, ≤128 chars)
    X-Timestamp   unix seconds (string, integer)
    X-Nonce       128-bit random hex/base64 — single use per (device, window)
    X-Signature   hex-encoded HMAC-SHA256 of the canonical string

Canonical string (single LF between fields, no trailing LF)
-----------------------------------------------------------
    METHOD              upper-case ("GET", "POST", ...)
    PATH                request path only — query is NOT included here
    CANONICAL_QUERY     sorted by key, then value; "k=v" pairs joined by '&';
                        URL-encoded with RFC-3986 unreserved set; '' for none
    BODY_SHA256_HEX     sha256 of the *raw* request body bytes ('' for empty)
    TIMESTAMP           same value as X-Timestamp header (verbatim string)
    NONCE               same value as X-Nonce header (verbatim string)
    DEVICE_ID           same value as X-Device-Id header (verbatim string)

Enforcement mode (env: REQUEST_SIGNING_MODE)
--------------------------------------------
    off       middleware short-circuits — no validation runs
    monitor   validate + emit structured log; never reject (safe rollout)
    enforce   validate + 401 on any failure

The middleware MUST run after RequestIdMiddleware so failures carry a
request-id, but before everything else so a forged request never reaches a
business handler. It is auth-aware in that it extracts `sub` from the Bearer
JWT to look up the signing key — but it does NOT enforce JWT validity (that
remains the dependency-layer job).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional
from urllib.parse import quote

import jwt
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.services.session_key_service import get_session_key

logger = get_logger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────

CLOCK_DRIFT_SECONDS = 300          # ±5 min
NONCE_TTL_SECONDS = 600            # 2× drift window
MAX_HEADER_LEN = 256
MAX_DEVICE_ID_LEN = 128

# Paths that MUST NOT be signature-checked:
#   * /auth/*       — bootstrap, no key issued yet
#   * /webhooks/*   — gateway-signed, separate verification
#   * /health, /metrics, /docs, /openapi.json — infra
#   * WebSocket upgrades — auth handled in /ws handshake
_SKIP_PREFIXES = (
    "/api/v1/auth/",
    "/api/v1/webhooks/",
    "/api/v1/health",
    "/health",
    "/metrics",
    "/docs",
    "/redoc",
    "/openapi.json",
)


def _is_skipped(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in _SKIP_PREFIXES)


def _safe_header(value: Optional[str], max_len: int = MAX_HEADER_LEN) -> Optional[str]:
    if value is None:
        return None
    if len(value) > max_len:
        return None
    return value


def _canonical_query(query_string: str) -> str:
    """Sort and percent-encode query params for the canonical string."""
    if not query_string:
        return ""
    pairs: list[tuple[str, str]] = []
    for raw_pair in query_string.split("&"):
        if not raw_pair:
            continue
        if "=" in raw_pair:
            k, v = raw_pair.split("=", 1)
        else:
            k, v = raw_pair, ""
        # Re-encode through a canonical alphabet so client/server agree on
        # whether '+' or '%20' represents a space, etc.
        pairs.append((quote(k, safe="-._~"), quote(v, safe="-._~")))
    pairs.sort()
    return "&".join(f"{k}={v}" for k, v in pairs)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest() if data else ""


def _extract_user_id(authorization_header: Optional[str]) -> Optional[str]:
    """Pull `sub` from the Bearer JWT without verifying — verification is
    delegated to the dependency layer. We only need the identifier so we can
    locate the signing key in Redis."""
    if not authorization_header:
        return None
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    if not token:
        return None
    try:
        # Verify=False is intentional — the real auth happens later; we only
        # use sub to look up the session signing key.
        payload = jwt.decode(token, options={"verify_signature": False, "verify_aud": False})
    except Exception:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) and sub else None


async def _nonce_seen(device_id: str, nonce: str) -> bool:
    """Return True if the nonce was already used inside the replay window.
    Atomic via SET NX so two parallel requests can't both win."""
    r = get_redis()
    key = f"sig-nonce:{device_id}:{nonce}"
    # set(..., nx=True) returns None if key already exists.
    set_ok = await r.set(key, "1", ex=NONCE_TTL_SECONDS, nx=True)
    return not set_ok


class RequestSecurityMiddleware(BaseHTTPMiddleware):
    """HMAC signature verification for every authenticated business request."""

    def __init__(self, app, mode: Optional[str] = None) -> None:
        super().__init__(app)
        self._configured_mode = (mode or "").lower().strip() or None

    @property
    def mode(self) -> str:
        if self._configured_mode in {"off", "monitor", "enforce"}:
            return self._configured_mode
        # Read lazily so env changes during tests are picked up.
        configured = getattr(get_settings(), "REQUEST_SIGNING_MODE", "monitor")
        configured = (configured or "monitor").lower().strip()
        if configured not in {"off", "monitor", "enforce"}:
            configured = "monitor"
        return configured

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        mode = self.mode
        if mode == "off":
            return await call_next(request)

        path = request.url.path
        if _is_skipped(path):
            return await call_next(request)

        # WebSocket upgrades & CORS preflight bypass.
        if request.method == "OPTIONS":
            return await call_next(request)
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        request_id = getattr(request.state, "request_id", None)
        verdict = await self._verify(request)

        if verdict["ok"]:
            return await call_next(request)

        # Failure path — log structured reason. In monitor mode pass through.
        log_payload = {
            "request_id": request_id,
            "path": path,
            "method": request.method,
            "reason": verdict["reason"],
            "device_id": verdict.get("device_id"),
            "user_id": verdict.get("user_id"),
            "mode": mode,
        }
        if mode == "enforce":
            logger.warning("request_signature_rejected", **log_payload)
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": "REQUEST_SIGNATURE_INVALID",
                        "message": "Request signature verification failed",
                        "details": {"reason": verdict["reason"]},
                        "retryable": False,
                    },
                    "request_id": request_id,
                },
            )
        # monitor mode — observe only.
        logger.info("request_signature_monitor_fail", **log_payload)
        return await call_next(request)

    # ── verification ─────────────────────────────────────────────────────

    async def _verify(self, request: Request) -> dict:
        h = request.headers
        device_id = _safe_header(h.get("X-Device-Id"), MAX_DEVICE_ID_LEN)
        ts = _safe_header(h.get("X-Timestamp"))
        nonce = _safe_header(h.get("X-Nonce"))
        signature = _safe_header(h.get("X-Signature"))

        if not (device_id and ts and nonce and signature):
            return {"ok": False, "reason": "missing_headers", "device_id": device_id}

        # 1. timestamp window
        try:
            ts_int = int(ts)
        except ValueError:
            return {"ok": False, "reason": "bad_timestamp", "device_id": device_id}
        skew = abs(time.time() - ts_int)
        if skew > CLOCK_DRIFT_SECONDS:
            return {"ok": False, "reason": "timestamp_out_of_window", "device_id": device_id}

        # 2. user id from JWT (no signing key for anonymous flows; those are
        # already in _SKIP_PREFIXES, so absence here is a hard fail).
        user_id = _extract_user_id(h.get("Authorization"))
        if not user_id:
            return {"ok": False, "reason": "no_subject", "device_id": device_id}

        # 3. signing key lookup
        try:
            signing_key = await get_session_key(user_id, device_id)
        except Exception as exc:  # redis hiccup — fail closed in enforce mode
            logger.warning("session_key_lookup_error", error=str(exc), user_id=user_id)
            return {
                "ok": False,
                "reason": "key_lookup_error",
                "device_id": device_id,
                "user_id": user_id,
            }
        if not signing_key:
            return {
                "ok": False,
                "reason": "no_session_key",
                "device_id": device_id,
                "user_id": user_id,
            }

        # 4. nonce replay check (do this AFTER signature check normally, but
        # nonce reuse is a hard fail regardless — and the SETNX is atomic).
        try:
            seen = await _nonce_seen(device_id, nonce)
        except Exception as exc:
            logger.warning("nonce_check_error", error=str(exc))
            return {
                "ok": False,
                "reason": "nonce_check_error",
                "device_id": device_id,
                "user_id": user_id,
            }
        if seen:
            return {
                "ok": False,
                "reason": "nonce_replayed",
                "device_id": device_id,
                "user_id": user_id,
            }

        # 5. compute expected HMAC over the canonical string.
        # NB: reading the body here would normally drain the ASGI receive
        # stream; we replay it via a wrapped receive() so downstream routes
        # still get the original payload.
        body = await request.body()

        async def _replay_receive():  # pragma: no cover - trivial closure
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = _replay_receive  # type: ignore[attr-defined]

        canonical = "\n".join(
            [
                request.method.upper(),
                request.url.path,
                _canonical_query(request.url.query),
                _sha256_hex(body),
                ts,
                nonce,
                device_id,
            ]
        )
        try:
            key_bytes = bytes.fromhex(signing_key)
        except ValueError:
            # Older clients may have stored a base64 key — accept both.
            try:
                key_bytes = base64.b64decode(signing_key)
            except Exception:
                return {
                    "ok": False,
                    "reason": "bad_key_encoding",
                    "device_id": device_id,
                    "user_id": user_id,
                }
        expected = hmac.new(key_bytes, canonical.encode("utf-8"), hashlib.sha256).hexdigest()

        # Accept both hex and base64 signatures from the client.
        provided_norm = signature.strip().lower()
        if not hmac.compare_digest(expected, provided_norm):
            # Try base64-encoded variant
            try:
                provided_bytes = base64.b64decode(signature, validate=False)
                if not hmac.compare_digest(
                    bytes.fromhex(expected), provided_bytes
                ):
                    return {
                        "ok": False,
                        "reason": "signature_mismatch",
                        "device_id": device_id,
                        "user_id": user_id,
                    }
            except Exception:
                return {
                    "ok": False,
                    "reason": "signature_mismatch",
                    "device_id": device_id,
                    "user_id": user_id,
                }

        return {"ok": True, "device_id": device_id, "user_id": user_id}
