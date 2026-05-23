"""
Refresh-token rotation + reuse detection.

Sits in front of Supabase's GoTrue refresh endpoint:

  client ──▶ POST /auth/token/refresh {refresh_token, device_id?}
            │
            ▼
  RefreshTokenService.before_refresh(...)         ── reuse-detection on the
            │                                       incoming hash; if the
            │                                       hash is already marked
            │                                       `rotated`, the entire
            │                                       active chain for that
            │                                       (user, device) is killed.
            ▼
  auth_service.refresh_token(...)                  ── proxies GoTrue
            │
            ▼
  RefreshTokenService.after_refresh(...)          ── records the freshly
                                                    minted token + flips the
                                                    incoming token to
                                                    rotated.

Tokens are NEVER stored raw — only sha256 hashes.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from app.core.database import get_service_connection
from app.core.logging import get_logger
from app.services.session_key_service import revoke_session_key

logger = get_logger(__name__)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ReuseDetected(Exception):
    """Raised when a refresh token that has already been rotated is replayed.
    The caller MUST reject the refresh and force re-authentication."""

    def __init__(self, user_id: Optional[str], device_id: Optional[str]):
        self.user_id = user_id
        self.device_id = device_id
        super().__init__("refresh_token_reuse_detected")


class RefreshTokenService:
    """Append-only ledger of every refresh token we've handed out."""

    async def record_issuance(
        self,
        *,
        user_id: str,
        device_id: str,
        token: str,
        parent_token: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        """Persist a freshly-issued refresh token. Idempotent on token_hash."""
        token_hash = _hash(token)
        parent_hash = _hash(parent_token) if parent_token else None
        async with get_service_connection() as conn:
            await conn.execute(
                """
                INSERT INTO refresh_tokens
                    (user_id, device_id, token_hash, parent_hash,
                     issued_at, last_seen_at, expires_at, ip, user_agent)
                VALUES ($1::uuid, $2, $3, $4, now(), now(), $5, $6, $7)
                ON CONFLICT (token_hash) DO UPDATE
                    SET last_seen_at = now()
                """,
                user_id,
                device_id,
                token_hash,
                parent_hash,
                expires_at,
                ip,
                user_agent,
            )
            if parent_hash:
                await conn.execute(
                    """
                    UPDATE refresh_tokens
                       SET rotated_to = $1,
                           revoked_at = COALESCE(revoked_at, now()),
                           revoked_reason = COALESCE(revoked_reason, 'rotated')
                     WHERE token_hash = $2
                    """,
                    token_hash,
                    parent_hash,
                )

    async def check_for_reuse(self, *, token: str) -> None:
        """Raise `ReuseDetected` if the incoming token was already rotated or
        revoked. Revokes the entire active chain for that (user, device).

        Unknown tokens (never seen) are allowed through — they belong to
        sessions established before this table existed."""
        token_hash = _hash(token)
        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT user_id::text AS user_id, device_id, revoked_at, revoked_reason
                  FROM refresh_tokens
                 WHERE token_hash = $1
                """,
                token_hash,
            )
            if row is None:
                return  # unknown token — let GoTrue arbitrate
            if row["revoked_at"] is None:
                # currently-active token — happy path
                await conn.execute(
                    "UPDATE refresh_tokens SET last_seen_at = now() WHERE token_hash = $1",
                    token_hash,
                )
                return

            # Replayed after revocation → kill the chain.
            killed = await conn.fetchval(
                "SELECT fn_refresh_token_revoke_chain($1::uuid, $2, 'reuse_detected')",
                row["user_id"],
                row["device_id"],
            )
            logger.warning(
                "refresh_token_reuse_detected",
                user_id=row["user_id"],
                device_id=row["device_id"],
                previous_reason=row["revoked_reason"],
                revoked_count=killed,
            )
            try:
                await revoke_session_key(row["user_id"], row["device_id"])
            except Exception as exc:  # best-effort
                logger.warning("session_key_revoke_failed", error=str(exc))
            raise ReuseDetected(row["user_id"], row["device_id"])

    async def revoke_for_logout(self, *, token: str) -> None:
        """Mark a refresh token as logged-out (no chain kill)."""
        token_hash = _hash(token)
        async with get_service_connection() as conn:
            await conn.execute(
                """
                UPDATE refresh_tokens
                   SET revoked_at = COALESCE(revoked_at, now()),
                       revoked_reason = COALESCE(revoked_reason, 'logout')
                 WHERE token_hash = $1
                """,
                token_hash,
            )


refresh_token_service = RefreshTokenService()


def parse_expires_at(payload: dict) -> Optional[datetime]:
    """Best-effort: convert GoTrue's `expires_at` (unix seconds) into UTC."""
    raw = payload.get("expires_at")
    if not raw:
        expires_in = payload.get("expires_in")
        if not expires_in:
            return None
        return datetime.now(timezone.utc) + _td(int(expires_in))
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (ValueError, TypeError):
        return None


def _td(seconds: int):
    from datetime import timedelta
    return timedelta(seconds=seconds)
