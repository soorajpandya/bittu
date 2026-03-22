"""
Upstash Redis REST Cache — Service.

REST-based Redis for subscription verification caching.
Used when local Redis is unavailable or as a supplementary cache layer.
"""
import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _cfg():
    return get_settings()


def _headers() -> dict:
    return {"Authorization": f"Bearer {_cfg().UPSTASH_REDIS_REST_TOKEN}"}


def _url() -> str:
    return _cfg().UPSTASH_REDIS_REST_URL


class UpstashCacheService:

    async def get(self, key: str) -> str | None:
        """GET a key from Upstash Redis."""
        url = _url()
        if not url:
            return None
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{url}/get/{key}", headers=_headers())
            resp.raise_for_status()
            data = resp.json()
        result = data.get("result")
        return result

    async def set(self, key: str, value: str, ttl: int = 300) -> bool:
        """SET a key with TTL in Upstash Redis."""
        url = _url()
        if not url:
            return False
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{url}/set/{key}/{value}/ex/{ttl}", headers=_headers())
            resp.raise_for_status()
        return True

    async def delete(self, key: str) -> bool:
        """DEL a key from Upstash Redis."""
        url = _url()
        if not url:
            return False
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{url}/del/{key}", headers=_headers())
            resp.raise_for_status()
        return True

    # ── Subscription cache helpers ──

    async def cache_subscription_status(self, user_id: str, is_active: bool) -> None:
        """Cache subscription verification result."""
        ttl = 300 if is_active else 60
        await self.set(f"sub:verify:{user_id}", "active" if is_active else "inactive", ttl=ttl)

    async def get_cached_subscription_status(self, user_id: str) -> bool | None:
        """Get cached subscription status. Returns None if not cached."""
        val = await self.get(f"sub:verify:{user_id}")
        if val is None:
            return None
        return val == "active"

    async def invalidate_subscription_cache(self, user_id: str) -> None:
        """Invalidate subscription cache after status change."""
        await self.delete(f"sub:verify:{user_id}")
