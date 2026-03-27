"""
Google Business Profile — Token Manager.

Handles:
  - Token storage and retrieval
  - Automatic refresh when expired (with distributed lock)
  - Secure DB persistence
  - Multi-tenant ownership validation
"""
import httpx
from datetime import datetime, timezone, timedelta

from app.core.config import get_settings
from app.core.database import get_connection
from app.core.logging import get_logger
from app.core.exceptions import UnauthorizedError, ForbiddenError
from app.core.redis import DistributedLock, LockError
from app.core.retry import retry_external_call

logger = get_logger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Refresh 5 minutes before actual expiry to avoid race conditions
EXPIRY_BUFFER = timedelta(minutes=5)
# Lock timeout for token refresh (prevent thundering herd)
REFRESH_LOCK_TIMEOUT = 15


class GoogleTokenManager:
    """Manages Google OAuth tokens — retrieval, refresh, and DB persistence."""

    # ── Public API ───────────────────────────────────────────

    async def get_connection_for_restaurant(
        self, user_id: str, restaurant_id: str
    ) -> dict | None:
        """Fetch the google_connections row for a user+restaurant."""
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, user_id, restaurant_id, access_token, refresh_token,
                       token_expiry, account_id, location_id, location_name, is_active,
                       last_locations_sync, last_reviews_sync, last_insights_sync, last_posts_sync
                FROM google_connections
                WHERE user_id = $1 AND (restaurant_id = $2 OR restaurant_id = $1) AND is_active = true
                ORDER BY CASE WHEN restaurant_id = $2 THEN 0 ELSE 1 END
                LIMIT 1
                """,
                user_id,
                restaurant_id,
            )
        return dict(row) if row else None

    async def verify_restaurant_ownership(self, user_id: str, restaurant_id: str) -> None:
        """
        Verify that user_id owns restaurant_id.
        Handles both cases: restaurant_id can be the actual restaurant UUID
        or the owner's user_id (legacy frontend behaviour).
        Raises ForbiddenError on cross-tenant access.
        """
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1 FROM restaurants
                WHERE (id = $1 OR owner_id = $1) AND owner_id = $2
                LIMIT 1
                """,
                restaurant_id,
                user_id,
            )
        if not row:
            logger.warning(
                "google_cross_tenant_blocked",
                user_id=user_id,
                restaurant_id=restaurant_id,
            )
            raise ForbiddenError("You do not have access to this restaurant.")

    async def force_refresh_token(self, user_id: str, restaurant_id: str) -> str:
        """Force a token refresh regardless of expiry. Used after a 401 from Google."""
        conn_row = await self.get_connection_for_restaurant(user_id, restaurant_id)
        if not conn_row:
            raise UnauthorizedError("Google account not connected.")
        return await self._refresh_token(conn_row)

    async def get_valid_token(self, user_id: str, restaurant_id: str) -> str:
        """
        Return a valid access_token, refreshing if necessary.
        Uses a distributed lock to prevent thundering-herd refresh.
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

        # ── Token needs refresh — acquire distributed lock ──
        lock_key = f"google_refresh:{user_id}:{restaurant_id}"
        try:
            async with DistributedLock(lock_key, timeout=REFRESH_LOCK_TIMEOUT, blocking_timeout=REFRESH_LOCK_TIMEOUT):
                # Re-check after acquiring lock (another request may have refreshed)
                conn_row = await self.get_connection_for_restaurant(user_id, restaurant_id)
                if not conn_row:
                    raise UnauthorizedError("Google account not connected.")

                expiry = conn_row["token_expiry"]
                if expiry and now < (expiry - EXPIRY_BUFFER):
                    return conn_row["access_token"]

                logger.info(
                    "google_token_refreshing",
                    user_id=user_id,
                    restaurant_id=restaurant_id,
                )
                return await self._refresh_token(conn_row)
        except LockError:
            # Another process is refreshing — wait briefly and retry read
            import asyncio
            await asyncio.sleep(2)
            conn_row = await self.get_connection_for_restaurant(user_id, restaurant_id)
            if conn_row and conn_row.get("access_token"):
                return conn_row["access_token"]
            raise UnauthorizedError("Token refresh in progress. Please retry.")

    async def store_tokens(
        self,
        user_id: str,
        restaurant_id: str,
        access_token: str,
        refresh_token: str,
        expires_in: int,
    ) -> dict:
        """Upsert tokens for a user+restaurant pair."""
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
                    sync_error    = NULL,
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
                WHERE user_id = $1 AND (restaurant_id = $2 OR restaurant_id = $1)
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
                WHERE user_id = $1 AND (restaurant_id = $2 OR restaurant_id = $1)
                """,
                user_id,
                restaurant_id,
            )
        logger.info("google_disconnected", user_id=user_id, restaurant_id=restaurant_id)

    async def update_sync_timestamp(
        self, user_id: str, restaurant_id: str, sync_type: str, error: str | None = None
    ) -> None:
        """Update the last sync timestamp for a specific sync type."""
        col_map = {
            "locations": "last_locations_sync",
            "reviews": "last_reviews_sync",
            "insights": "last_insights_sync",
            "posts": "last_posts_sync",
        }
        col = col_map.get(sync_type)
        if not col:
            return
        async with get_connection() as conn:
            await conn.execute(
                f"""
                UPDATE google_connections
                SET {col} = now(), sync_error = $3, updated_at = now()
                WHERE user_id = $1 AND (restaurant_id = $2 OR restaurant_id = $1)
                """,
                user_id,
                restaurant_id,
                error,
            )

    async def get_all_active_connections(self) -> list[dict]:
        """Return all active connections (for background sync jobs)."""
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, restaurant_id, account_id, location_id, location_name
                FROM google_connections
                WHERE is_active = true AND account_id IS NOT NULL AND location_id IS NOT NULL
                """
            )
        return [dict(r) for r in rows]

    # ── Private ──────────────────────────────────────────────

    @retry_external_call(max_attempts=2, min_wait=1, max_wait=5)
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
                restaurant_id=conn_row["restaurant_id"],
            )
            # Mark connection as broken so user re-authenticates
            await self._mark_sync_error(
                conn_row["user_id"],
                conn_row["restaurant_id"],
                f"Token refresh failed: HTTP {resp.status_code}",
            )
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
                SET access_token = $3, token_expiry = $4, sync_error = NULL, updated_at = now()
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

    async def _mark_sync_error(self, user_id: str, restaurant_id: str, error: str) -> None:
        async with get_connection() as conn:
            await conn.execute(
                """
                UPDATE google_connections
                SET sync_error = $3, updated_at = now()
                WHERE user_id = $1 AND restaurant_id = $2
                """,
                user_id,
                restaurant_id,
                error,
            )
