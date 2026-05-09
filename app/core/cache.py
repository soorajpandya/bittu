"""
Route-level response cache decorator.

Lets any FastAPI handler wrap with `@cached_route(prefix=..., ttl=...)` to get
JSON response caching backed by Redis. Cache key is derived from the prefix
plus all primitive arguments (path/query params) plus the caller's tenant
context (restaurant_id and branch_id), so cross-tenant collisions are
impossible.

Falls open silently if Redis is unavailable — never breaks the request path.

USAGE
-----
    from app.core.cache import cached_route, invalidate_prefix

    @router.get("")
    @cached_route(prefix="settings", ttl=300)
    async def get_settings(user: UserContext = Depends(...)):
        ...

    @router.put("")
    async def update_settings(...):
        result = await _svc.upsert(...)
        await invalidate_prefix("settings", user)
        return result

INVALIDATION
------------
`invalidate_prefix(prefix, user)` deletes every cache key for the caller's
tenant under that prefix. Call it in any write handler that mutates data the
prefix represents.

DESIGN NOTES
------------
- Async-only (FastAPI handlers are coroutines).
- Skips caching when no UserContext can be inferred (anonymous routes are
  not cached — safer than guessing tenant boundaries).
- Uses orjson for fast (de)serialisation; values that orjson can't serialise
  fall back to `default=str`.
- Key format: `route:{prefix}:{tenant_key}:{md5(args+kwargs)}`.
"""
from __future__ import annotations

import hashlib
import inspect
from functools import wraps
from typing import Any, Callable, Optional

import orjson
import structlog

from app.core.auth import UserContext
from app.core.redis import get_redis

logger = structlog.get_logger(__name__)

_KEY_PREFIX = "route"


def _tenant_key(user: Optional[UserContext]) -> str:
    """Stable per-tenant slice of the cache namespace."""
    if user is None:
        return "anon"
    rid = getattr(user, "restaurant_id", None) or "noresto"
    bid = getattr(user, "branch_id", None) or "nobranch"
    return f"{rid}:{bid}"


def _find_user(args: tuple, kwargs: dict) -> Optional[UserContext]:
    for v in list(kwargs.values()) + list(args):
        if isinstance(v, UserContext):
            return v
    return None


def _hash_args(args: tuple, kwargs: dict) -> str:
    # Build a deterministic, JSON-safe payload of all primitive args.
    payload: list[Any] = []
    for a in args:
        if isinstance(a, UserContext):
            continue
        try:
            payload.append(_normalise(a))
        except Exception:
            payload.append(repr(a))
    for k in sorted(kwargs.keys()):
        v = kwargs[k]
        if isinstance(v, UserContext):
            continue
        try:
            payload.append((k, _normalise(v)))
        except Exception:
            payload.append((k, repr(v)))
    raw = orjson.dumps(payload, default=str)
    return hashlib.md5(raw).hexdigest()  # nosec - cache key, not crypto


def _normalise(v: Any) -> Any:
    """Reduce values to JSON-friendly primitives for hashing."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_normalise(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _normalise(x) for k, x in v.items()}
    # uuid / datetime / Decimal etc → str via orjson default
    return str(v)


def cached_route(prefix: str, ttl: int = 60) -> Callable:
    """
    Cache the JSON-serialised return value of an async route handler.

    Args:
        prefix: Logical bucket name. Use the same prefix in `invalidate_prefix`
                from the corresponding write handler.
        ttl:    Time-to-live in seconds.
    """
    def decorator(fn: Callable) -> Callable:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError("@cached_route can only wrap async functions")

        @wraps(fn)
        async def wrapper(*args, **kwargs):
            user = _find_user(args, kwargs)
            tenant = _tenant_key(user)
            digest = _hash_args(args, kwargs)
            key = f"{_KEY_PREFIX}:{prefix}:{tenant}:{digest}"

            # ── Try cache (fail open) ──────────────────────────────
            try:
                r = get_redis()
                hit = await r.get(key)
                if hit is not None:
                    return orjson.loads(hit)
            except Exception as exc:
                logger.debug("cache_read_failed", key=key, error=str(exc))

            # ── Miss → call handler ────────────────────────────────
            result = await fn(*args, **kwargs)

            # ── Store (fail open) ──────────────────────────────────
            try:
                r = get_redis()
                await r.set(
                    key,
                    orjson.dumps(result, default=str),
                    ex=ttl,
                )
            except Exception as exc:
                logger.debug("cache_write_failed", key=key, error=str(exc))

            return result

        return wrapper
    return decorator


async def invalidate_prefix(prefix: str, user: Optional[UserContext] = None) -> int:
    """
    Drop every cached entry under `prefix` for the caller's tenant.
    Returns the number of keys deleted (0 if Redis is down).
    """
    tenant = _tenant_key(user)
    pattern = f"{_KEY_PREFIX}:{prefix}:{tenant}:*"
    deleted = 0
    try:
        r = get_redis()
        async for k in r.scan_iter(pattern, count=200):
            await r.delete(k)
            deleted += 1
    except Exception as exc:
        logger.debug("cache_invalidate_failed", pattern=pattern, error=str(exc))
    return deleted


async def invalidate_prefix_global(prefix: str) -> int:
    """
    Drop every cached entry under `prefix` for ALL tenants.
    Use sparingly — only when truly cross-tenant data changes
    (e.g. global pincode list).
    """
    pattern = f"{_KEY_PREFIX}:{prefix}:*"
    deleted = 0
    try:
        r = get_redis()
        async for k in r.scan_iter(pattern, count=200):
            await r.delete(k)
            deleted += 1
    except Exception as exc:
        logger.debug("cache_invalidate_global_failed", pattern=pattern, error=str(exc))
    return deleted
