"""
Google Business Profile — Token Manager.

Handles:
  - Token storage and retrieval
  - Automatic refresh when expired
  - Secure DB persistence
"""
import httpx
from datetime import datetime, timezone, timedelta

from app.core.config import get_settings
from app.core.database import get_connection
from app.core.logging import get_logger
from app.core.exceptions import UnauthorizedError

logger = get_logger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Refresh 5 minutes before actual expiry to avoid race conditions
EXPIRY_BUFFER = timedelta(minutes=5)


class GoogleTokenManager:
    """Manages Google OAuth tokens — retrieval, refresh, and DB persistence."""

    async def get_connection_for_restaurant(
        self, user_id: str, restaurant_id: str
    ) -> dict | None:
        """Fetch the google_connections row for a user+restaurant."""
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, user_id, restaurant_id, access_token, refresh_token,
                       token_expiry, account_id, location_id, location_name, is_active
                FROM google_connections
                WHERE user_id = $1 AND restaurant_id = $2 AND is_active = true
                """,
                user_id,
                restaurant_id,
            )
        return dict(row) if row else None

    async def get_valid_token(self, user_id: str, restaurant_id: str) -> str:
        """
        Return a valid access_token, refreshing if necessary.
        Raises UnauthorizedError if no connection or refresh fails.
        """
        conn_row = await self.get_connection_for_restaurant(user_id, restaurant_id)
        if not conn_row:
            raise UnauthorizedError(
                "Google account not connected. Please connect via /google/connect."
            )

        expiry = conn_row["token_expiry"]
        now = datetime.now(timezone.utc)

        if expiry and now < (expiry - EXPIRY_BUFFER):
            return conn_row["access_token"]

        # Needs refresh
        logger.info(
            "google_token_refreshing",
            user_id=user_id,
            restaurant_id=restaurant_id,
        )
        return await self._refresh_token(conn_row)

    async def store_tokens(
        self,
        user_id: str,
        restaurant_id: str,
        access_token: str,
        refresh_token: str,
        expires_in: int,
    ) -> dict:
        """
        Upsert tokens for a user+restaurant pair.
        Returns the stored row.
        """
        expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO google_connections
                    (user_id, restaurant_id, access_token, refresh_token, token_expiry)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id, restaurant_id) DO UPDATE SET
                    access_token  = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    token_expiry  = EXCLUDED.token_expiry,
                    is_active     = true,
                    updated_at    = now()
                RETURNING *
                """,
                user_id,
                restaurant_id,
                access_token,
                refresh_token,
                expiry,
            )
        logger.info("google_tokens_stored", user_id=user_id, restaurant_id=restaurant_id)
        return dict(row)

    async def update_account_location(
        self,
        user_id: str,
        restaurant_id: str,
        account_id: str,
        location_id: str | None = None,
        location_name: str | None = None,
    ) -> None:
        """Persist the chosen Google account/location IDs."""
        async with get_connection() as conn:
            await conn.execute(
                """
                UPDATE google_connections
                SET account_id     = $3,
                    location_id    = $4,
                    location_name  = $5,
                    updated_at     = now()
                WHERE user_id = $1 AND restaurant_id = $2
                """,
                user_id,
                restaurant_id,
                account_id,
                location_id,
                location_name,
            )

    async def disconnect(self, user_id: str, restaurant_id: str) -> None:
        """Soft-delete a connection."""
        async with get_connection() as conn:
            await conn.execute(
                """
                UPDATE google_connections
                SET is_active = false, updated_at = now()
                WHERE user_id = $1 AND restaurant_id = $2
                """,
                user_id,
                restaurant_id,
            )
        logger.info("google_disconnected", user_id=user_id, restaurant_id=restaurant_id)

    # ── Private ──────────────────────────────────────────────

    async def _refresh_token(self, conn_row: dict) -> str:
        """Exchange refresh_token for a new access_token and persist it."""
        settings = get_settings()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": settings.GOOGLE_BUSINESS_CLIENT_ID,
                    "client_secret": settings.GOOGLE_BUSINESS_CLIENT_SECRET,
                    "refresh_token": conn_row["refresh_token"],
                    "grant_type": "refresh_token",
                },
            )

        if resp.status_code != 200:
            logger.error(
                "google_token_refresh_failed",
                status=resp.status_code,
                body=resp.text,
                user_id=conn_row["user_id"],
            )
            # Mark connection as inactive so user re-authenticates
            await self.disconnect(conn_row["user_id"], conn_row["restaurant_id"])
            raise UnauthorizedError(
                "Google token refresh failed. Please reconnect your Google account."
            )

        data = resp.json()
        new_access_token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        new_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        # Persist new token
        async with get_connection() as conn:
            await conn.execute(
                """
                UPDATE google_connections
                SET access_token = $3, token_expiry = $4, updated_at = now()
                WHERE user_id = $1 AND restaurant_id = $2
                """,
                conn_row["user_id"],
                conn_row["restaurant_id"],
                new_access_token,
                new_expiry,
            )

        logger.info(
            "google_token_refreshed",
            user_id=conn_row["user_id"],
            restaurant_id=conn_row["restaurant_id"],
        )
        return new_access_token
