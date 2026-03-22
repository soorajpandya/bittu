"""
Redis connection management for caching, pub/sub, rate limiting, and locking.
"""
import redis.asyncio as redis
from typing import Optional

from app.core.config import get_settings

_redis: Optional[redis.Redis] = None
_pubsub_redis: Optional[redis.Redis] = None


async def init_redis():
    """Initialize Redis connection pools."""
    global _redis, _pubsub_redis
    settings = get_settings()
    _redis = redis.from_url(
        settings.REDIS_URL,
        max_connections=settings.REDIS_MAX_CONNECTIONS,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
        retry_on_timeout=True,
    )
    # Separate pool for pub/sub to avoid blocking main pool
    _pubsub_redis = redis.from_url(
        settings.REDIS_URL,
        max_connections=20,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    await _redis.ping()


async def close_redis():
    """Gracefully close Redis connections."""
    global _redis, _pubsub_redis
    if _redis:
        await _redis.aclose()
        _redis = None
    if _pubsub_redis:
        await _pubsub_redis.aclose()
        _pubsub_redis = None


def get_redis() -> redis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized. Call init_redis() first.")
    return _redis


def get_pubsub_redis() -> redis.Redis:
    if _pubsub_redis is None:
        raise RuntimeError("Redis pub/sub not initialized.")
    return _pubsub_redis


# ── Distributed Lock ──

class DistributedLock:
    """
    Redis-based distributed lock for critical sections.
    Prevents double-processing of orders, payments, etc.
    """

    def __init__(self, key: str, timeout: int = 10, blocking_timeout: int = 5):
        self.key = f"lock:{key}"
        self.timeout = timeout
        self.blocking_timeout = blocking_timeout
        self._lock = None

    async def __aenter__(self):
        r = get_redis()
        self._lock = r.lock(
            self.key,
            timeout=self.timeout,
            blocking_timeout=self.blocking_timeout,
        )
        acquired = await self._lock.acquire()
        if not acquired:
            raise LockError(f"Could not acquire lock: {self.key}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._lock:
            try:
                await self._lock.release()
            except Exception:
                pass  # Lock may have already expired


class LockError(Exception):
    """Raised when a distributed lock cannot be acquired."""
    pass


# ── Idempotency ──

async def check_idempotency(key: str, ttl: int = 3600) -> Optional[str]:
    """
    Check if an operation was already processed.
    Returns cached result if exists, None if new operation.
    """
    r = get_redis()
    return await r.get(f"idempotent:{key}")


async def set_idempotency(key: str, result: str, ttl: int = 3600):
    """Mark an operation as processed with its result."""
    r = get_redis()
    await r.set(f"idempotent:{key}", result, ex=ttl)


# ── Rate Limiting ──

async def check_rate_limit(identifier: str, limit: int, window: int = 60) -> bool:
    """
    Sliding window rate limiter.
    Returns True if within limit, False if exceeded.
    """
    r = get_redis()
    key = f"ratelimit:{identifier}"
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, window)
    results = await pipe.execute()
    current_count = results[0]
    return current_count <= limit


# ── Cache Helpers ──

async def cache_get(key: str) -> Optional[str]:
    r = get_redis()
    return await r.get(f"cache:{key}")


async def cache_set(key: str, value: str, ttl: int = 300):
    r = get_redis()
    await r.set(f"cache:{key}", value, ex=ttl)


async def cache_delete(key: str):
    r = get_redis()
    await r.delete(f"cache:{key}")


async def cache_delete_pattern(pattern: str):
    """Invalidate all cache keys matching pattern."""
    r = get_redis()
    async for key in r.scan_iter(f"cache:{pattern}"):
        await r.delete(key)
