"""Endpoint-scope DI gate.

Usage:
    from app.dependencies.scopes import require_scope
    from app.core.scopes import PLATFORM_PAYOUTS_APPROVE

    @router.post("/payouts/{id}/approve",
                 dependencies=[require_scope(PLATFORM_PAYOUTS_APPROVE)])
    async def approve(...): ...

The scope→role resolution is delegated to app.core.auth (legacy
ROLE_PERMISSIONS during Phase 1; pluggable matrix afterwards).

See docs/ARCHITECTURE_V2.md §5 + §7.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.core.auth import UserContext, get_current_user, ROLE_PERMISSIONS


def _scope_matches(role_perms: set[str], required: str) -> bool:
    """Match `required` against a wildcard-aware role permission set.

    A role perm of "orders:*" matches required "orders:read".
    A role perm of "*" matches anything.
    Domain-prefixed scopes ("merchant:orders:read") fall back to the
    last two segments for legacy roles.
    """
    if "*" in role_perms or required in role_perms:
        return True
    # wildcard: "domain:*"
    head = required.rsplit(":", 1)[0]
    if f"{head}:*" in role_perms:
        return True
    # legacy fallback: drop leading audience prefix ("merchant:orders:read"
    # → "orders:read") so existing ROLE_PERMISSIONS sets continue to apply.
    parts = required.split(":")
    if len(parts) == 3:
        legacy = ":".join(parts[1:])
        if legacy in role_perms or f"{parts[1]}:*" in role_perms:
            return True
    return False


def require_scope(scope: str):
    """Build a FastAPI dependency that asserts the caller has `scope`."""

    async def _dep(user: UserContext = Depends(get_current_user)) -> UserContext:
        role = (user.role or "").lower()
        perms = ROLE_PERMISSIONS.get(role, set())
        if not _scope_matches(perms, scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing_scope:{scope}",
            )
        return user

    return Depends(_dep)
