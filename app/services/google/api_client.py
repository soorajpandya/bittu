"""
Google Business Profile — API Client.

Shared HTTP client wrapper with:
  - Automatic token refresh on 401
  - Retry with exponential backoff
  - Redis caching layer
  - Structured logging for every call
"""
import hashlib
import httpx
import orjson
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis import cache_get, cache_set, cache_delete_pattern
from app.core.exceptions import AppException
from app.core.retry import retry_external_call
from app.services.google.token_manager import GoogleTokenManager

logger = get_logger(__name__)

_token_mgr = GoogleTokenManager()

# Google API base URLs
ACCOUNT_MGMT_BASE = "https://mybusinessaccountmanagement.googleapis.com/v1"
BUSINESS_INFO_BASE = "https://mybusinessbusinessinformation.googleapis.com/v1"
MY_BUSINESS_BASE = "https://mybusiness.googleapis.com/v4"
PERFORMANCE_BASE = "https://businessprofileperformance.googleapis.com/v1"


def _cache_key(prefix: str, restaurant_id: str, extra: str = "") -> str:
    """Build a deterministic cache key."""
    raw = f"gbp:{prefix}:{restaurant_id}"
    if extra:
        raw += f":{hashlib.md5(extra.encode()).hexdigest()[:12]}"
    return raw


class GoogleAPIClient:
    """
    Centralized Google API client.
    All Google HTTP calls go through this to get:
      - auto token refresh on 401
      - retry on transient errors
      - optional caching
    """

    @retry_external_call(max_attempts=3, min_wait=0.5, max_wait=10)
    async def request(
        self,
        method: str,
        url: str,
        user_id: str,
        restaurant_id: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        cache_key: str | None = None,
        cache_ttl: int = 0,
        timeout: int = 20,
    ) -> dict:
        """
        Make an authenticated Google API request.

        If cache_key and cache_ttl are set, will return cached response on GET.
        On 401, refreshes token and retries once.
        """
        # ── Check cache (GET only) ──
        if cache_key and cache_ttl and method.upper() == "GET":
            cached = await cache_get(cache_key)
            if cached:
                logger.debug("google_api_cache_hit", cache_key=cache_key)
                return orjson.loads(cached)

        access_token = await _token_mgr.get_valid_token(user_id, restaurant_id)

        resp = await self._do_request(method, url, access_token, params=params, json_body=json_body, timeout=timeout)

        # ── Handle 401 — token may have expired between check and use ──
        if resp.status_code == 401:
            logger.info(
                "google_api_401_retrying",
                url=url,
                user_id=user_id,
                restaurant_id=restaurant_id,
            )
            # Force refresh
            access_token = await _token_mgr.get_valid_token(user_id, restaurant_id)
            resp = await self._do_request(method, url, access_token, params=params, json_body=json_body, timeout=timeout)

        # ── Handle errors ──
        if resp.status_code == 429:
            logger.warning(
                "google_api_rate_limited",
                url=url,
                user_id=user_id,
                restaurant_id=restaurant_id,
            )
            raise AppException(
                status_code=429,
                detail="Google API rate limit reached. Please try again shortly.",
                error_code="GOOGLE_RATE_LIMITED",
            )

        if resp.status_code >= 400:
            logger.error(
                "google_api_error",
                method=method,
                url=url,
                status=resp.status_code,
                body=resp.text[:500],
                user_id=user_id,
                restaurant_id=restaurant_id,
            )
            raise AppException(
                status_code=resp.status_code,
                detail=f"Google API error: {resp.text[:300]}",
                error_code="GOOGLE_API_ERROR",
            )

        data = resp.json()

        # ── Store in cache ──
        if cache_key and cache_ttl and method.upper() == "GET":
            await cache_set(cache_key, orjson.dumps(data).decode(), ttl=cache_ttl)

        return data

    async def _do_request(
        self,
        method: str,
        url: str,
        access_token: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        timeout: int = 20,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {access_token}"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
            )

    async def invalidate_cache(self, prefix: str, restaurant_id: str) -> None:
        """Invalidate all cached data for a resource type + restaurant."""
        pattern = f"gbp:{prefix}:{restaurant_id}*"
        await cache_delete_pattern(pattern)


# Singleton
google_api = GoogleAPIClient()
