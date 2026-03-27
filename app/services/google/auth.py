"""
Google Business Profile — OAuth Authentication Service.

Handles:
  - OAuth URL generation with secure server-side state
  - Callback code exchange with anti-replay validation
  - One-time-use state tokens with TTL
"""
import secrets
import urllib.parse
import httpx
from datetime import datetime, timezone, timedelta

from app.core.config import get_settings
from app.core.database import get_connection
from app.core.logging import get_logger
from app.core.exceptions import ValidationError, ForbiddenError
from app.core.events import DomainEvent, emit_and_publish
from app.core.retry import retry_external_call
from app.services.google.token_manager import GoogleTokenManager

logger = get_logger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/business.manage"

# State expires after 10 minutes
STATE_TTL = timedelta(minutes=10)

token_mgr = GoogleTokenManager()


class GoogleAuthService:
    """OAuth 2.0 flow for Google Business Profile with hardened security."""

    async def generate_auth_url(
        self, user_id: str, restaurant_id: str, redirect_uri: str | None = None
    ) -> dict:
        """
        Build the Google OAuth consent URL.
        State is stored server-side with nonce, user_id, and TTL.
        """
        settings = get_settings()
        nonce = secrets.token_urlsafe(32)
        state = secrets.token_urlsafe(32)
        resolved_redirect = redirect_uri or settings.GOOGLE_BUSINESS_REDIRECT_URI
        expires_at = datetime.now(timezone.utc) + STATE_TTL

        # Store state server-side
        async with get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO google_oauth_states (state, user_id, restaurant_id, nonce, redirect_uri, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                state,
                user_id,
                restaurant_id,
                nonce,
                resolved_redirect,
                expires_at,
            )

        params = {
            "client_id": settings.GOOGLE_BUSINESS_CLIENT_ID,
            "redirect_uri": resolved_redirect,
            "response_type": "code",
            "scope": GOOGLE_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"

        logger.info(
            "google_oauth_url_generated",
            user_id=user_id,
            restaurant_id=restaurant_id,
        )
        return {"auth_url": url, "state": state}

    async def handle_callback(
        self, code: str, state: str, user_id: str, redirect_uri: str | None = None
    ) -> dict:
        """
        Exchange authorization code for tokens and store them.
        Validates state is unused, unexpired, and matches the calling user.
        """
        # ── Validate state ──
        async with get_connection() as conn:
            state_row = await conn.fetchrow(
                """
                SELECT user_id, restaurant_id, nonce, redirect_uri, expires_at, used
                FROM google_oauth_states
                WHERE state = $1
                """,
                state,
            )

        if not state_row:
            logger.warning("google_oauth_invalid_state", state=state[:16], user_id=user_id)
            raise ValidationError("Invalid OAuth state parameter.")

        if state_row["used"]:
            logger.warning("google_oauth_replayed_state", user_id=user_id)
            raise ValidationError("OAuth state has already been used.")

        if datetime.now(timezone.utc) > state_row["expires_at"]:
            logger.warning("google_oauth_expired_state", user_id=user_id)
            raise ValidationError("OAuth state has expired. Please try again.")

        if state_row["user_id"] != user_id:
            logger.warning(
                "google_oauth_user_mismatch",
                expected=state_row["user_id"],
                got=user_id,
            )
            raise ForbiddenError("OAuth state does not belong to this user.")

        restaurant_id = state_row["restaurant_id"]
        resolved_redirect = redirect_uri or state_row["redirect_uri"]

        # ── Invalidate state (one-time use) ──
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE google_oauth_states SET used = true WHERE state = $1",
                state,
            )

        # ── Exchange code for tokens ──
        data = await self._exchange_code(code, resolved_redirect)

        access_token = data["access_token"]
        refresh_token = data.get("refresh_token", "")
        expires_in = data.get("expires_in", 3600)

        if not refresh_token:
            logger.warning(
                "google_no_refresh_token",
                user_id=user_id,
                hint="User may have already granted access. Revoke and retry.",
            )

        connection = await token_mgr.store_tokens(
            user_id=user_id,
            restaurant_id=restaurant_id,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
        )

        # ── Emit event ──
        await emit_and_publish(DomainEvent(
            event_type="google.connected",
            payload={"restaurant_id": restaurant_id, "connection_id": str(connection["id"])},
            user_id=user_id,
            restaurant_id=restaurant_id,
        ))

        logger.info("google_oauth_complete", user_id=user_id, restaurant_id=restaurant_id)
        return {
            "connected": True,
            "restaurant_id": restaurant_id,
            "id": str(connection["id"]),
        }

    @retry_external_call(max_attempts=2, min_wait=1, max_wait=5)
    async def _exchange_code(self, code: str, redirect_uri: str) -> dict:
        """Exchange authorization code for tokens with retry."""
        settings = get_settings()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_BUSINESS_CLIENT_ID,
                    "client_secret": settings.GOOGLE_BUSINESS_CLIENT_SECRET,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )

        if resp.status_code != 200:
            logger.error("google_oauth_exchange_failed", status=resp.status_code, body=resp.text)
            raise ValidationError(f"Google OAuth token exchange failed: {resp.text}")

        return resp.json()

    async def cleanup_expired_states(self) -> int:
        """Remove expired OAuth states. Called by background job."""
        async with get_connection() as conn:
            result = await conn.execute(
                "DELETE FROM google_oauth_states WHERE expires_at < now()"
            )
        count = int(result.split()[-1]) if result else 0
        if count:
            logger.info("google_oauth_states_cleaned", count=count)
        return count
