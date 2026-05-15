"""
RazorpayClient — single chokepoint for all outbound Razorpay HTTPS calls.

Responsibilities
----------------
* Basic-auth header (key_id : key_secret) constructed once per request.
* `httpx.AsyncClient` with sane timeouts, kept warm via lazy module-level instance.
* Exponential backoff w/ jitter on 429/5xx/network errors (configurable).
* Razorpay request-level idempotency key support: when caller passes
  `idempotency_key=...`, we set the `X-Razorpay-Idempotency` header AND look
  up `rzp_api_idempotency` first — if a prior successful response is cached,
  we return it without ever hitting Razorpay (replay-safe).
* Forensic audit: every attempt is recorded in `rzp_api_calls` (append-only,
  partitioned). Sensitive headers are scrubbed.
* Structured logging via `app.core.logging` (operation, status, attempt,
  duration_ms, correlation_id).
* Typed exception hierarchy so callers can `except RazorpayRateLimitedError`.

Phase 1 deliverable. Higher-level service modules (orders.py, payments.py …)
sit on top of this and never instantiate their own httpx clients.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import time
import uuid
from contextvars import ContextVar
from typing import Any, Mapping, Optional

import httpx

from app.core.config import get_settings
from app.core.database import get_service_connection
from app.core.logging import get_logger

logger = get_logger(__name__)

RAZORPAY_BASE = "https://api.razorpay.com"
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
DEFAULT_MAX_RETRIES = 4              # 1 attempt + up to 4 retries
DEFAULT_BACKOFF_BASE = 0.5           # seconds
DEFAULT_BACKOFF_CAP = 8.0
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
SENSITIVE_HEADERS = {"authorization", "x-razorpay-account", "cookie", "set-cookie"}

# Correlation id propagation (set by request middleware where available).
correlation_id_ctx: ContextVar[Optional[str]] = ContextVar(
    "razorpay_correlation_id", default=None
)


# ───────────────────────── exceptions ─────────────────────────────────────


class RazorpayError(Exception):
    """Base for all Razorpay client failures."""


class RazorpayAPIError(RazorpayError):
    """Razorpay returned a structured error response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        error_code: Optional[str] = None,
        error_description: Optional[str] = None,
        raw_body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.error_description = error_description
        self.raw_body = raw_body


class RazorpayBadRequestError(RazorpayAPIError):
    """4xx (non-429) — caller bug, do NOT retry."""


class RazorpayRateLimitedError(RazorpayAPIError):
    """429 — retried internally; raised only when retries exhausted."""


class RazorpayServerError(RazorpayAPIError):
    """5xx — retried internally; raised only when retries exhausted."""


# ───────────────────────── helpers ────────────────────────────────────────


def _auth_header(settings) -> str:
    creds = f"{settings.RAZORPAY_KEY_ID}:{settings.RAZORPAY_KEY_SECRET}".encode()
    return "Basic " + base64.b64encode(creds).decode()


def _scrub_headers(headers: Mapping[str, str]) -> dict:
    return {
        k: ("***" if k.lower() in SENSITIVE_HEADERS else v)
        for k, v in headers.items()
    }


def _backoff_seconds(attempt: int, retry_after: Optional[float] = None) -> float:
    """Exponential backoff w/ full jitter, honours Retry-After when present."""
    if retry_after is not None:
        return min(max(retry_after, 0.0), DEFAULT_BACKOFF_CAP * 2)
    raw = DEFAULT_BACKOFF_BASE * (2 ** (attempt - 1))
    return random.uniform(0, min(raw, DEFAULT_BACKOFF_CAP))


def _canonical_request_hash(body: Any) -> str:
    payload = json.dumps(body or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


# ───────────────────────── client ─────────────────────────────────────────


class RazorpayClient:
    """
    Async Razorpay client. Reusable across the process. Safe to share — one
    underlying httpx.AsyncClient with a connection pool.

    Use :func:`get_razorpay_client` for the process-wide singleton instead of
    constructing this directly.
    """

    def __init__(
        self,
        *,
        base_url: str = RAZORPAY_BASE,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(timeout=timeout, base_url=self._base_url)
        self._lock = asyncio.Lock()
        self._closed = False

    # ── lifecycle ────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()

    # ── public verbs ─────────────────────────────────────────────────────

    async def get(
        self,
        path: str,
        *,
        operation: str,
        params: Optional[Mapping[str, Any]] = None,
        merchant_id: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> dict:
        return await self._request(
            "GET", path, operation=operation, params=params,
            merchant_id=merchant_id, account_id=account_id,
        )

    async def post(
        self,
        path: str,
        *,
        operation: str,
        json_body: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        merchant_id: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> dict:
        return await self._request(
            "POST", path, operation=operation, json_body=json_body,
            idempotency_key=idempotency_key,
            merchant_id=merchant_id, account_id=account_id,
        )

    async def patch(
        self,
        path: str,
        *,
        operation: str,
        json_body: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        merchant_id: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> dict:
        return await self._request(
            "PATCH", path, operation=operation, json_body=json_body,
            idempotency_key=idempotency_key,
            merchant_id=merchant_id, account_id=account_id,
        )

    async def delete(
        self,
        path: str,
        *,
        operation: str,
        merchant_id: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> dict:
        return await self._request(
            "DELETE", path, operation=operation,
            merchant_id=merchant_id, account_id=account_id,
        )

    # ── core ─────────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        merchant_id: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> dict:
        if self._closed:
            raise RazorpayError("RazorpayClient is closed")

        settings = get_settings()
        if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
            raise RazorpayError("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not configured")

        # ── idempotency cache hit (caller-supplied key) ──
        if idempotency_key:
            cached = await self._lookup_idempotent(operation, idempotency_key)
            if cached is not None:
                logger.info(
                    "razorpay_idempotent_hit",
                    operation=operation, idempotency_key=idempotency_key,
                )
                return cached

        # ── prepare headers ──
        headers: dict[str, str] = {
            "Authorization": _auth_header(settings),
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["X-Razorpay-Idempotency"] = idempotency_key
        if account_id:
            # Used by Route APIs to act on behalf of a linked account.
            headers["X-Razorpay-Account"] = account_id
        correlation_id = correlation_id_ctx.get() or str(uuid.uuid4())

        api_call_id = uuid.uuid4()
        request_hash = _canonical_request_hash(json_body)
        last_exc: Optional[Exception] = None
        last_response: Optional[httpx.Response] = None

        # ── insert audit row (state=pending) ──
        await self._insert_api_call(
            api_call_id=api_call_id,
            merchant_id=merchant_id,
            operation=operation,
            method=method,
            path=path,
            idempotency_key=idempotency_key,
            request_body=dict(json_body) if json_body else None,
            request_headers=_scrub_headers(headers),
            correlation_id=correlation_id,
        )

        for attempt in range(1, self._max_retries + 2):  # 1 + retries
            t0 = time.perf_counter()
            try:
                resp = await self._client.request(
                    method, path,
                    params=dict(params) if params else None,
                    json=dict(json_body) if json_body else None,
                    headers=headers,
                )
                duration_ms = int((time.perf_counter() - t0) * 1000)
                last_response = resp

                if resp.status_code in RETRYABLE_STATUSES and attempt <= self._max_retries:
                    retry_after = _parse_retry_after(resp.headers.get("retry-after"))
                    sleep_for = _backoff_seconds(attempt, retry_after)
                    logger.warning(
                        "razorpay_retry",
                        operation=operation, status=resp.status_code,
                        attempt=attempt, sleep_s=round(sleep_for, 3),
                        correlation_id=correlation_id,
                    )
                    await self._update_api_call_attempt(
                        api_call_id, attempt=attempt,
                        response_status=resp.status_code,
                        duration_ms=duration_ms,
                        state="retrying",
                    )
                    await asyncio.sleep(sleep_for)
                    continue

                # ── terminal response ──
                body = _safe_json(resp)
                if 200 <= resp.status_code < 300:
                    await self._finalise_api_call(
                        api_call_id, response_status=resp.status_code,
                        response_body=body,
                        response_headers=_scrub_headers(resp.headers),
                        duration_ms=duration_ms,
                        attempt=attempt,
                        state="succeeded",
                    )
                    if idempotency_key:
                        await self._store_idempotent(
                            operation, idempotency_key, api_call_id,
                            response_body=body, response_status=resp.status_code,
                        )
                    logger.info(
                        "razorpay_call_ok",
                        operation=operation, status=resp.status_code,
                        attempt=attempt, duration_ms=duration_ms,
                        correlation_id=correlation_id,
                    )
                    return body

                # error response
                err_code, err_desc = _extract_error(body)
                await self._finalise_api_call(
                    api_call_id, response_status=resp.status_code,
                    response_body=body,
                    response_headers=_scrub_headers(resp.headers),
                    duration_ms=duration_ms,
                    attempt=attempt,
                    state="failed",
                    error_code=err_code,
                    error_message=err_desc,
                )
                logger.warning(
                    "razorpay_call_error",
                    operation=operation, status=resp.status_code,
                    error_code=err_code, attempt=attempt,
                    correlation_id=correlation_id,
                )
                raise _classify_error(resp.status_code, err_code, err_desc, body)

            except (httpx.TransportError, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                duration_ms = int((time.perf_counter() - t0) * 1000)
                if attempt > self._max_retries:
                    await self._finalise_api_call(
                        api_call_id, response_status=None, response_body=None,
                        response_headers=None, duration_ms=duration_ms,
                        attempt=attempt, state="failed",
                        error_code="network", error_message=str(exc),
                    )
                    logger.error(
                        "razorpay_network_failed",
                        operation=operation, attempt=attempt,
                        error=str(exc), correlation_id=correlation_id,
                    )
                    raise RazorpayError(f"network failure: {exc}") from exc
                sleep_for = _backoff_seconds(attempt)
                logger.warning(
                    "razorpay_network_retry",
                    operation=operation, attempt=attempt,
                    sleep_s=round(sleep_for, 3), error=str(exc),
                    correlation_id=correlation_id,
                )
                await self._update_api_call_attempt(
                    api_call_id, attempt=attempt,
                    response_status=None, duration_ms=duration_ms,
                    state="retrying",
                )
                await asyncio.sleep(sleep_for)

        # Defensive: loop should always return or raise.
        raise RazorpayError(
            f"unreachable: retries exhausted operation={operation} last_exc={last_exc}"
        )

    # ── persistence helpers ─────────────────────────────────────────────

    async def _insert_api_call(
        self,
        *,
        api_call_id: uuid.UUID,
        merchant_id: Optional[str],
        operation: str,
        method: str,
        path: str,
        idempotency_key: Optional[str],
        request_body: Optional[dict],
        request_headers: dict,
        correlation_id: str,
    ) -> None:
        try:
            async with get_service_connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO rzp_api_calls (
                        id, merchant_id, operation, method, path,
                        idempotency_key, request_body, request_headers,
                        state, attempt, correlation_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb,
                              'pending', 1, $9)
                    """,
                    api_call_id, merchant_id, operation, method, path,
                    idempotency_key,
                    json.dumps(request_body) if request_body else None,
                    json.dumps(request_headers),
                    correlation_id,
                )
        except Exception:  # pragma: no cover — never let audit kill the call
            logger.exception("razorpay_audit_insert_failed", operation=operation)

    async def _update_api_call_attempt(
        self,
        api_call_id: uuid.UUID,
        *,
        attempt: int,
        response_status: Optional[int],
        duration_ms: int,
        state: str,
    ) -> None:
        try:
            async with get_service_connection() as conn:
                await conn.execute(
                    """
                    UPDATE rzp_api_calls
                       SET attempt         = $2,
                           response_status = $3,
                           duration_ms     = $4,
                           state           = $5::rzp_api_call_state
                     WHERE id = $1
                    """,
                    api_call_id, attempt, response_status, duration_ms, state,
                )
        except Exception:  # pragma: no cover
            logger.exception("razorpay_audit_update_failed")

    async def _finalise_api_call(
        self,
        api_call_id: uuid.UUID,
        *,
        response_status: Optional[int],
        response_body: Any,
        response_headers: Optional[dict],
        duration_ms: int,
        attempt: int,
        state: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        try:
            async with get_service_connection() as conn:
                await conn.execute(
                    """
                    UPDATE rzp_api_calls
                       SET response_status  = $2,
                           response_body    = $3::jsonb,
                           response_headers = $4::jsonb,
                           duration_ms      = $5,
                           attempt          = $6,
                           state            = $7::rzp_api_call_state,
                           error_code       = $8,
                           error_message    = $9,
                           completed_at     = NOW()
                     WHERE id = $1
                    """,
                    api_call_id,
                    response_status,
                    json.dumps(response_body) if response_body is not None else None,
                    json.dumps(response_headers) if response_headers else None,
                    duration_ms, attempt, state, error_code, error_message,
                )
        except Exception:  # pragma: no cover
            logger.exception("razorpay_audit_finalise_failed")

    async def _lookup_idempotent(
        self, operation: str, idempotency_key: str
    ) -> Optional[dict]:
        try:
            async with get_service_connection() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT response_body, response_status
                      FROM rzp_api_idempotency
                     WHERE operation = $1 AND idempotency_key = $2
                    """,
                    operation, idempotency_key,
                )
                if row and row["response_status"] and 200 <= row["response_status"] < 300:
                    body = row["response_body"]
                    if isinstance(body, str):
                        body = json.loads(body)
                    return body
        except Exception:  # pragma: no cover
            logger.exception("razorpay_idempotent_lookup_failed")
        return None

    async def _store_idempotent(
        self,
        operation: str,
        idempotency_key: str,
        api_call_id: uuid.UUID,
        *,
        response_body: Any,
        response_status: int,
    ) -> None:
        try:
            async with get_service_connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO rzp_api_idempotency (
                        operation, idempotency_key, api_call_id,
                        response_body, response_status
                    ) VALUES ($1, $2, $3, $4::jsonb, $5)
                    ON CONFLICT (operation, idempotency_key) DO NOTHING
                    """,
                    operation, idempotency_key, api_call_id,
                    json.dumps(response_body) if response_body is not None else None,
                    response_status,
                )
        except Exception:  # pragma: no cover
            logger.exception("razorpay_idempotent_store_failed")


# ───────────────────────── module helpers ─────────────────────────────────


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"_raw_text": resp.text[:4096]}


def _extract_error(body: Any) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(body, dict):
        return None, None
    err = body.get("error") or {}
    if isinstance(err, dict):
        return err.get("code"), err.get("description") or err.get("reason")
    return None, None


def _classify_error(
    status: int, code: Optional[str], desc: Optional[str], body: Any
) -> RazorpayAPIError:
    msg = f"razorpay {status} {code or ''} {desc or ''}".strip()
    if status == 429:
        return RazorpayRateLimitedError(
            msg, status_code=status, error_code=code,
            error_description=desc, raw_body=body,
        )
    if 500 <= status < 600:
        return RazorpayServerError(
            msg, status_code=status, error_code=code,
            error_description=desc, raw_body=body,
        )
    return RazorpayBadRequestError(
        msg, status_code=status, error_code=code,
        error_description=desc, raw_body=body,
    )


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


# ───────────────────────── singleton accessor ─────────────────────────────

_singleton: Optional[RazorpayClient] = None
_singleton_lock = asyncio.Lock()


async def get_razorpay_client() -> RazorpayClient:
    """Return the process-wide RazorpayClient (lazy-initialised)."""
    global _singleton
    if _singleton is None:
        async with _singleton_lock:
            if _singleton is None:
                _singleton = RazorpayClient()
    return _singleton


async def shutdown_razorpay_client() -> None:
    """Call from FastAPI lifespan on shutdown."""
    global _singleton
    if _singleton is not None:
        await _singleton.aclose()
        _singleton = None
