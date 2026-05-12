"""Endpoint-scope DI gates (Phase-1, platform-aware).

Three flavors:

    require_scope("merchant:orders:write")
        Any authenticated user whose role grants the scope.

    require_platform_scope("platform:payouts:approve")
        Caller must be a platform admin (row in platform_admin_users)
        AND have the scope in their platform-role bundle.

    resolve_claims
        Plain dependency that returns the request Claims object.

See docs/ARCHITECTURE_V2.md §5 + §7.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.core.auth import UserContext, get_current_user
from app.core.claims import Claims, build_claims


# ── platform-admin lookup ───────────────────────────────────────────
async def _is_platform_admin(user_id: str) -> bool:
    from app.core.database import get_connection  # avoid cycle
    try:
        async with get_connection() as c:
            return bool(await c.fetchval(
                "SELECT fn_is_platform_admin($1::uuid)", user_id
            ))
    except Exception:
        # Fail-closed: if the helper isn't deployed, no platform access.
        return False


async def resolve_claims(
    user: UserContext = Depends(get_current_user),
) -> Claims:
    is_admin = await _is_platform_admin(user.user_id)
    return build_claims(user, is_platform_admin=is_admin)


def require_scope(scope: str):
    """Generic scope gate. Works for merchant + branch + platform users."""

    async def _dep(claims: Claims = Depends(resolve_claims)) -> Claims:
        if not claims.has_scope(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing_scope:{scope}",
            )
        return claims

    return Depends(_dep)


def require_platform_scope(scope: str):
    """Platform-only scope gate. Caller must be platform admin."""

    async def _dep(claims: Claims = Depends(resolve_claims)) -> Claims:
        if not claims.is_platform_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="platform_admin_required",
            )
        if not claims.has_scope(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing_scope:{scope}",
            )
        return claims

    return Depends(_dep)
