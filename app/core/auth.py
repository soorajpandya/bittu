"""
Authentication and authorization.
Validates Supabase JWTs, resolves user context, enforces RBAC.
Supports both HS256 (legacy) and ES256 (current) Supabase JWT signing.
"""
import time
import threading
import jwt
import httpx
from jwt import PyJWKClient
from fastapi import Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
from dataclasses import dataclass

from app.core.config import get_settings
from app.core.exceptions import UnauthorizedError, ForbiddenError
from app.core.database import get_connection
from app.core.redis import cache_get, cache_set
from app.core.logging import get_logger

logger = get_logger(__name__)
security = HTTPBearer()

# ── JWKS cache (for ES256 tokens) ──
_jwks_client: Optional[PyJWKClient] = None
_jwks_lock = threading.Lock()


def _get_jwks_client() -> PyJWKClient:
    """Lazy-init a cached PyJWKClient pointing at Supabase JWKS endpoint."""
    global _jwks_client
    if _jwks_client is None:
        with _jwks_lock:
            if _jwks_client is None:
                s = get_settings()
                jwks_url = f"{s.SUPABASE_URL}/auth/v1/.well-known/jwks.json"
                _jwks_client = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
    return _jwks_client


def _decode_token(token: str) -> dict:
    """
    Decode a Supabase JWT supporting both ES256 and HS256 algorithms.
    - ES256: fetches public key from Supabase JWKS endpoint (cached)
    - HS256: uses SUPABASE_JWT_SECRET from settings
    """
    # Peek at the header to determine algorithm
    try:
        header = jwt.get_unverified_header(token)
    except jwt.DecodeError:
        raise UnauthorizedError("Malformed token")

    alg = header.get("alg", "HS256")

    if alg == "ES256":
        client = _get_jwks_client()
        signing_key = client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256"],
            audience="authenticated",
        )
    else:
        # HS256 fallback (older Supabase projects)
        return jwt.decode(
            token,
            get_settings().SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
        )


@dataclass
class UserContext:
    """Authenticated user context available in every request."""
    user_id: str
    email: Optional[str] = None
    role: str = "owner"  # owner | manager | cashier | chef | waiter | staff
    restaurant_id: Optional[str] = None
    branch_id: Optional[str] = None
    owner_id: Optional[str] = None  # For branch users, the owner who created them
    is_branch_user: bool = False


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UserContext:
    """
    Decode and validate Supabase JWT.
    Resolve restaurant/branch context from DB (cached in Redis).
    """
    token = credentials.credentials
    try:
        payload = _decode_token(token)
    except jwt.ExpiredSignatureError:
        raise UnauthorizedError("Token expired")
    except jwt.InvalidTokenError:
        raise UnauthorizedError("Invalid token")

    user_id = payload.get("sub")
    if not user_id:
        raise UnauthorizedError("Invalid token: no subject")

    email = payload.get("email")

    # Check Redis cache for user context
    cache_key = f"user_ctx:{user_id}"
    try:
        cached = await cache_get(cache_key)
    except Exception:
        cached = None
    if cached:
        import orjson
        data = orjson.loads(cached)
        return UserContext(**data)

    # Resolve from DB (graceful: if DB is down, return minimal context from JWT)
    try:
        ctx = await _resolve_user_context(user_id, email)
    except Exception as exc:
        logger.warning("user_context_resolve_failed", user_id=user_id, error=str(exc))
        return UserContext(user_id=user_id, email=email)

    # Cache for 5 minutes
    import orjson
    try:
        await cache_set(cache_key, orjson.dumps({
            "user_id": ctx.user_id,
            "email": ctx.email,
            "role": ctx.role,
            "restaurant_id": ctx.restaurant_id,
            "branch_id": ctx.branch_id,
            "owner_id": ctx.owner_id,
            "is_branch_user": ctx.is_branch_user,
        }).decode(), ttl=300)
    except Exception:
        pass

    return ctx


async def _resolve_user_context(user_id: str, email: Optional[str]) -> UserContext:
    """
    Determine if user is an owner or branch user.
    Owner: has a restaurant in `restaurants` table.
    Branch user: exists in `branch_users` table.
    """
    async with get_connection() as conn:
        # Check if branch user first (more specific)
        branch_user = await conn.fetchrow(
            """
            SELECT bu.user_id, bu.branch_id, bu.owner_id, bu.role, bu.is_active,
                   sb.restaurant_id
            FROM branch_users bu
            JOIN sub_branches sb ON sb.id = bu.branch_id
            WHERE bu.user_id = $1 AND bu.is_active = true
            """,
            user_id,
        )

        if branch_user:
            return UserContext(
                user_id=user_id,
                email=email,
                role=branch_user["role"],
                restaurant_id=str(branch_user["restaurant_id"]) if branch_user["restaurant_id"] else None,
                branch_id=str(branch_user["branch_id"]),
                owner_id=str(branch_user["owner_id"]),
                is_branch_user=True,
            )

        # Check if owner (has restaurant)
        restaurant = await conn.fetchrow(
            """
            SELECT r.id as restaurant_id, sb.id as branch_id
            FROM restaurants r
            LEFT JOIN sub_branches sb ON sb.restaurant_id = r.id AND sb.is_main_branch = true
            WHERE r.owner_id = $1
            LIMIT 1
            """,
            user_id,
        )

        if restaurant:
            return UserContext(
                user_id=user_id,
                email=email,
                role="owner",
                restaurant_id=str(restaurant["restaurant_id"]),
                branch_id=str(restaurant["branch_id"]) if restaurant["branch_id"] else None,
                owner_id=user_id,
                is_branch_user=False,
            )

        # New user — no restaurant yet
        return UserContext(user_id=user_id, email=email)


# ── RBAC Enforcement ──

ROLE_HIERARCHY = {
    "owner": 100,
    "manager": 80,
    "cashier": 60,
    "chef": 40,
    "waiter": 30,
    "staff": 20,
}

# Permissions: role → set of allowed actions
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": {
        "orders:*", "payments:*", "menu:*", "inventory:*", "staff:*",
        "reports:*", "settings:*", "delivery:*", "tables:*", "kitchen:*",
        "customers:*", "coupons:*", "subscriptions:*", "branches:*",
        "kyc:*", "voice:*", "ai:*", "notifications:*",
        "accounting:*",
    },
    "manager": {
        "orders:*", "payments:*", "menu:read", "menu:write", "inventory:*",
        "staff:read", "reports:read", "delivery:*", "tables:*", "kitchen:*",
        "customers:*", "coupons:read", "kyc:*", "notifications:*",
        "accounting:read", "accounting:write",
    },
    "cashier": {
        "orders:read", "orders:write", "payments:*", "tables:*", "customers:read",
        "customers:write", "coupons:read",
        "accounting:read",
    },
    "chef": {
        "kitchen:*", "orders:read", "inventory:read",
    },
    "waiter": {
        "orders:read", "orders:write", "tables:*", "kitchen:read", "customers:read",
    },
    "staff": {
        "orders:read", "tables:read", "kitchen:read",
    },
}


def require_role(*allowed_roles: str):
    """Dependency that checks if the user has one of the allowed roles."""
    async def _check(user: UserContext = Depends(get_current_user)) -> UserContext:
        if user.role not in allowed_roles:
            raise ForbiddenError(
                f"Role '{user.role}' is not allowed. Required: {', '.join(allowed_roles)}"
            )
        return user
    return _check


def require_permission(permission: str):
    """Dependency that checks if the user's role has a specific permission.
    Accepts both colon and dot separators: 'payments:create' or 'payments.create'.
    """
    async def _check(user: UserContext = Depends(get_current_user)) -> UserContext:
        user_perms = ROLE_PERMISSIONS.get(user.role, set())
        # Normalise to colon separator for matching
        norm = permission.replace(".", ":")
        resource = norm.split(":")[0]
        if norm not in user_perms and f"{resource}:*" not in user_perms:
            raise ForbiddenError(f"Permission denied: {permission}")
        return user
    return _check


def require_min_role(min_role: str):
    """Dependency that checks if the user's role level meets the minimum."""
    async def _check(user: UserContext = Depends(get_current_user)) -> UserContext:
        user_level = ROLE_HIERARCHY.get(user.role, 0)
        required_level = ROLE_HIERARCHY.get(min_role, 0)
        if user_level < required_level:
            raise ForbiddenError(f"Minimum role required: {min_role}")
        return user
    return _check


# ── Standalone helpers for WebSocket auth (no DI) ──

def decode_jwt(token: str) -> dict:
    """Decode and validate a JWT outside of FastAPI DI (e.g. WebSocket)."""
    return _decode_token(token)


async def resolve_user_context(user_id: str) -> UserContext:
    """Resolve user context by user_id — used by WebSocket handler."""
    return await _resolve_user_context(user_id, email=None)
