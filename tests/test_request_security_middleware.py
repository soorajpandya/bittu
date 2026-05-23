"""
Tests for RequestSecurityMiddleware (HMAC X-Signature verification).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from unittest.mock import AsyncMock, patch

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.middleware.request_security import (
    RequestSecurityMiddleware,
    _canonical_query,
    _sha256_hex,
)

DEVICE_ID = "device-test-001"
USER_ID = "00000000-0000-0000-0000-000000000001"
SIGNING_KEY = "00" * 32  # 64-char hex


def _build_app() -> Starlette:
    async def echo(request):  # pragma: no cover - trivial
        body = await request.body()
        return JSONResponse({"len": len(body)})

    app = Starlette(routes=[Route("/api/v1/ping", endpoint=echo, methods=["POST"])])
    app.add_middleware(RequestSecurityMiddleware, mode="enforce")
    return app


def _make_jwt(sub: str) -> str:
    """Construct an unsigned JWT — middleware only reads `sub` (verify=False)."""
    import base64
    import json

    def b64(d: bytes) -> str:
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = b64(json.dumps({"sub": sub}).encode())
    return f"{header}.{payload}."


def _sign(method: str, path: str, query: str, body: bytes, ts: str, nonce: str, device: str, key_hex: str) -> str:
    canonical = "\n".join([
        method.upper(),
        path,
        _canonical_query(query),
        _sha256_hex(body),
        ts,
        nonce,
        device,
    ])
    return hmac.new(bytes.fromhex(key_hex), canonical.encode(), hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def _redis_mock():
    """Patch get_session_key + Redis nonce SETNX."""
    nonce_seen = {}

    async def _set(key, value, ex=None, nx=False):
        if key.startswith("sig-nonce:") and nx:
            if key in nonce_seen:
                return None
            nonce_seen[key] = value
            return True
        return True

    redis_stub = AsyncMock()
    redis_stub.set = AsyncMock(side_effect=_set)

    with patch("app.middleware.request_security.get_redis", return_value=redis_stub), \
         patch("app.middleware.request_security.get_session_key", new=AsyncMock(return_value=SIGNING_KEY)):
        yield


def test_valid_signature_passes():
    app = _build_app()
    body = b'{"hello":"world"}'
    ts = str(int(time.time()))
    nonce = "nonce-abc-1"
    sig = _sign("POST", "/api/v1/ping", "", body, ts, nonce, DEVICE_ID, SIGNING_KEY)

    with TestClient(app) as c:
        r = c.post(
            "/api/v1/ping",
            content=body,
            headers={
                "Authorization": f"Bearer {_make_jwt(USER_ID)}",
                "X-Device-Id": DEVICE_ID,
                "X-Timestamp": ts,
                "X-Nonce": nonce,
                "X-Signature": sig,
            },
        )
    assert r.status_code == 200
    assert r.json() == {"len": len(body)}


def test_missing_headers_rejected():
    app = _build_app()
    with TestClient(app) as c:
        r = c.post("/api/v1/ping", content=b"{}", headers={"Authorization": f"Bearer {_make_jwt(USER_ID)}"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "REQUEST_SIGNATURE_INVALID"


def test_timestamp_out_of_window_rejected():
    app = _build_app()
    body = b"{}"
    ts = str(int(time.time()) - 10_000)
    nonce = "n2"
    sig = _sign("POST", "/api/v1/ping", "", body, ts, nonce, DEVICE_ID, SIGNING_KEY)
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/ping",
            content=body,
            headers={
                "Authorization": f"Bearer {_make_jwt(USER_ID)}",
                "X-Device-Id": DEVICE_ID,
                "X-Timestamp": ts,
                "X-Nonce": nonce,
                "X-Signature": sig,
            },
        )
    assert r.status_code == 401
    assert r.json()["error"]["details"]["reason"] == "timestamp_out_of_window"


def test_replayed_nonce_rejected():
    app = _build_app()
    body = b"{}"
    ts = str(int(time.time()))
    nonce = "nonce-replay"
    sig = _sign("POST", "/api/v1/ping", "", body, ts, nonce, DEVICE_ID, SIGNING_KEY)
    headers = {
        "Authorization": f"Bearer {_make_jwt(USER_ID)}",
        "X-Device-Id": DEVICE_ID,
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-Signature": sig,
    }
    with TestClient(app) as c:
        r1 = c.post("/api/v1/ping", content=body, headers=headers)
        r2 = c.post("/api/v1/ping", content=body, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 401
    assert r2.json()["error"]["details"]["reason"] == "nonce_replayed"


def test_bad_signature_rejected():
    app = _build_app()
    body = b"{}"
    ts = str(int(time.time()))
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/ping",
            content=body,
            headers={
                "Authorization": f"Bearer {_make_jwt(USER_ID)}",
                "X-Device-Id": DEVICE_ID,
                "X-Timestamp": ts,
                "X-Nonce": "n-bad-sig",
                "X-Signature": "deadbeef" * 8,
            },
        )
    assert r.status_code == 401
    assert r.json()["error"]["details"]["reason"] == "signature_mismatch"


def test_monitor_mode_passes_through():
    app = Starlette(routes=[
        Route("/api/v1/ping", endpoint=lambda r: JSONResponse({"ok": True}), methods=["POST"]),
    ])
    app.add_middleware(RequestSecurityMiddleware, mode="monitor")
    with TestClient(app) as c:
        r = c.post("/api/v1/ping", content=b"{}")  # no signature headers
    assert r.status_code == 200


def test_auth_route_skipped_in_enforce():
    """Auth bootstrap endpoints must NEVER be HMAC-checked."""
    async def login(request):  # pragma: no cover - trivial
        return JSONResponse({"token": "x"})

    app = Starlette(routes=[Route("/api/v1/auth/google/callback", endpoint=login, methods=["POST"])])
    app.add_middleware(RequestSecurityMiddleware, mode="enforce")
    with TestClient(app) as c:
        r = c.post("/api/v1/auth/google/callback", json={"code": "x"})
    assert r.status_code == 200
