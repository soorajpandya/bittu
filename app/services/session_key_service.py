"""
Session signing-key management for HMAC request signing.

The Flutter client (and any future SDK) attaches `X-Signature` / `X-Nonce` /
`X-Timestamp` / `X-Device-Id` headers to every request. The HMAC is computed
with a *per-session, per-device* signing key that the backend issues at login
and rotates on every refresh.

Storage
-------
Redis key:   `session-signing:{user_id}:{device_id}`
Value:       hex-encoded 32-byte secret
TTL:         `SESSION_SIGNING_KEY_TTL_SECONDS` (default 30 days)

This module never exposes the raw key after issuance — callers must persist
the response body in client-side secure storage (Keychain / Keystore).

The verification path (request_security middleware) is the ONLY other reader.
"""
from __future__ import annotations

import secrets
from typing import Optional

from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger(__name__)

# 30 days — slightly longer than the longest expected refresh-token lifetime
# so an in-flight refresh never finds a missing key. Rotated on every refresh.
DEFAULT_TTL_SECONDS = 30 * 24 * 3600


def _redis_key(user_id: str, device_id: str) -> str:
    # Both user_id and device_id are caller-supplied — guard against ':' confusion.
    safe_device = device_id.replace(":", "_")
    return f"session-signing:{user_id}:{safe_device}"


def _generate_key() -> str:
    """256-bit secret, hex-encoded (64 chars). Hex keeps it transport-safe."""
    return secrets.token_hex(32)


async def issue_session_key(
    user_id: str,
    device_id: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """
    Mint a fresh signing key and persist it in Redis. Returns the raw key.
    Overwrites any existing key for the same (user_id, device_id).
    """
    if not user_id or not device_id:
        raise ValueError("user_id and device_id are required")
    key = _generate_key()
    r = get_redis()
    await r.set(_redis_key(user_id, device_id), key, ex=ttl_seconds)
    logger.info(
        "session_signing_key_issued",
        user_id=user_id,
        device_id=device_id,
        ttl=ttl_seconds,
    )
    return key


async def rotate_session_key(
    user_id: str,
    device_id: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """Alias for issue_session_key — semantic clarity at refresh time."""
    return await issue_session_key(user_id, device_id, ttl_seconds=ttl_seconds)


async def get_session_key(user_id: str, device_id: str) -> Optional[str]:
    """Return the current signing key or None when absent/expired."""
    if not user_id or not device_id:
        return None
    r = get_redis()
    return await r.get(_redis_key(user_id, device_id))


async def revoke_session_key(user_id: str, device_id: str) -> None:
    """Drop the signing key (logout, token-reuse anomaly, manual revoke)."""
    if not user_id or not device_id:
        return
    r = get_redis()
    await r.delete(_redis_key(user_id, device_id))
    logger.info("session_signing_key_revoked", user_id=user_id, device_id=device_id)


async def revoke_all_for_user(user_id: str) -> int:
    """Nuke every signing key for the user (e.g. on password change / breach)."""
    if not user_id:
        return 0
    r = get_redis()
    pattern = f"session-signing:{user_id}:*"
    count = 0
    async for key in r.scan_iter(pattern):
        await r.delete(key)
        count += 1
    if count:
        logger.warning("session_signing_keys_purged", user_id=user_id, count=count)
    return count
