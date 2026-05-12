"""JWT claim model + scope resolution for the v2 architecture.

This module is the single source of truth for *what shape* a Bittu JWT
takes and *which scopes* the holder has.

In Phase-1 it is a thin wrapper that derives scopes from existing
`UserContext` fields (role + platform-admin flag). In later phases we
move scope generation server-side at JWT mint time and read them
directly from the token.

See docs/ARCHITECTURE_V2.md §7 (RBAC).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from app.core.auth import ROLE_PERMISSIONS, UserContext


# ── Platform tier ────────────────────────────────────────────────────
# Platform admins (rows in `platform_admin_users`) get every scope in
# the platform: namespace plus financial:read for inspection.
#
# Sub-roles (finance_admin / recon_admin / risk_admin / support_admin)
# can be introduced later by extending PLATFORM_SUBROLE_SCOPES; until
# then any platform admin gets the full bundle.
PLATFORM_ADMIN_SCOPES: frozenset[str] = frozenset({
    "platform:*",
    "financial:ledger:read",
    "financial:journal:post",          # write only via service token + this scope
    "financial:payout:orchestrate",
    "financial:refund:orchestrate",
})

PLATFORM_SUBROLE_SCOPES: dict[str, frozenset[str]] = {
    "super_admin":     PLATFORM_ADMIN_SCOPES,
    "finance_admin":   frozenset({
        "platform:fee_plans:write", "platform:payouts:approve",
        "platform:escrow:read", "platform:fin_reports:read",
        "platform:audit:read", "financial:ledger:read",
    }),
    "recon_admin":     frozenset({
        "platform:recon:operate", "platform:audit:read",
        "platform:escrow:read", "financial:ledger:read",
    }),
    "risk_admin":      frozenset({
        "platform:risk:operate", "platform:disputes:operate",
        "platform:audit:read", "platform:fin_reports:read",
    }),
    "support_admin":   frozenset({
        "platform:merchants:read", "platform:audit:read",
        "platform:refunds:operate",
    }),
}


@dataclass
class Claims:
    """Resolved request identity. Built per-request; do not cache."""
    user_id: str
    email: str | None
    role: str                             # legacy role (owner/manager/...)
    platform_role: str | None = None      # super_admin / finance_admin / ...
    is_platform_admin: bool = False
    merchant_id: str | None = None
    branch_id: str | None = None
    scopes: frozenset[str] = field(default_factory=frozenset)

    def has_scope(self, required: str) -> bool:
        return _scope_matches(self.scopes, required)


def _scope_matches(scopes: Iterable[str], required: str) -> bool:
    s = set(scopes)
    if "*" in s or required in s:
        return True
    head = required.rsplit(":", 1)[0]
    if f"{head}:*" in s:
        return True
    # platform:* matches platform:anything (one extra level)
    parts = required.split(":")
    if len(parts) >= 2 and f"{parts[0]}:*" in s:
        return True
    # legacy-role fallback: drop audience prefix ("merchant:orders:read"
    # → "orders:read") so existing ROLE_PERMISSIONS sets continue to apply.
    if len(parts) == 3:
        legacy = ":".join(parts[1:])
        if legacy in s or f"{parts[1]}:*" in s:
            return True
    return False


def build_claims(
    user: UserContext,
    *,
    is_platform_admin: bool,
    platform_role: str | None = None,
) -> Claims:
    """Resolve a UserContext + platform-admin flag into a Claims object."""
    role = (user.role or "").lower()
    scopes: set[str] = set(ROLE_PERMISSIONS.get(role, set()))

    if is_platform_admin:
        # Until per-platform-subrole assignment lands, super_admin bundle.
        sub = (platform_role or "super_admin").lower()
        scopes.update(PLATFORM_SUBROLE_SCOPES.get(sub, PLATFORM_ADMIN_SCOPES))

    return Claims(
        user_id=user.user_id,
        email=user.email,
        role=role,
        platform_role=platform_role,
        is_platform_admin=is_platform_admin,
        merchant_id=user.restaurant_id,
        branch_id=user.branch_id,
        scopes=frozenset(scopes),
    )
