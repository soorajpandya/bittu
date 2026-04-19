import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import orjson

from app.core.database import get_connection
from app.core.logging import get_logger
from app.core.redis import cache_delete, cache_get, cache_set
from app.schemas.rbac import PermissionDecision

if TYPE_CHECKING:
    from app.core.auth import UserContext

logger = get_logger(__name__)


# Lightweight process cache to avoid redis/db traffic on hot paths.
_LOCAL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LOCAL_TTL_SECONDS = 30


class RBACService:
    def __init__(self) -> None:
        self._fallback_permissions = {
            "owner": {
                "order.*", "orders.*", "billing.*", "payment.*", "payments.*",
                "table.*", "tables.*", "inventory.*", "voice.use", "kitchen.*",
                "kitchen_station.*",
            },
            "manager": {
                "order.create", "order.edit", "order.cancel", "order.read",
                "orders.create", "orders.read", "orders.update",
                "billing.generate", "billing.discount",
                "payment.create", "payments.create",
                "table.read", "table.start", "table.close", "table.manage", "tables.manage",
                "inventory.read", "inventory.update", "inventory.manage",
                "kitchen.read", "kitchen.update",
                "kitchen_station.read", "kitchen_station.manage",
            },
            "cashier": {
                "order.read", "order.edit", "orders.read", "orders.update",
                "billing.generate", "billing.discount",
                "payment.create", "payments.create",
                "table.read", "table.start", "table.close", "table.manage", "tables.manage",
            },
            "waiter": {
                "order.create", "order.read", "orders.create", "orders.read",
                "table.read", "table.start", "table.close", "table.manage", "tables.manage", "kitchen.read",
            },
            "chef": {"order.read", "orders.read", "kitchen.read", "kitchen.update", "kitchen_station.read"},
            "kitchen": {"order.read", "orders.read", "kitchen.read", "kitchen.update", "kitchen_station.read"},
            "staff": {"order.read", "orders.read", "table.read", "kitchen.read"},
        }

    def _norm(self, permission_key: str) -> str:
        return permission_key.strip().lower().replace(":", ".")

    def _aliases(self, permission_key: str) -> set[str]:
        base = self._norm(permission_key)
        aliases = {base}

        # Keep compatibility between singular/plural legacy keys.
        if base.startswith("order."):
            aliases.add("orders." + base.split(".", 1)[1])
        if base.startswith("orders."):
            aliases.add("order." + base.split(".", 1)[1])
        if base.startswith("payment."):
            aliases.add("payments." + base.split(".", 1)[1])
        if base.startswith("payments."):
            aliases.add("payment." + base.split(".", 1)[1])
        if base.startswith("table."):
            aliases.add("tables." + base.split(".", 1)[1])
        if base.startswith("tables."):
            aliases.add("table." + base.split(".", 1)[1])

        return aliases

    def _has_wildcard(self, key: str, candidates: set[str]) -> bool:
        resource = key.split(".", 1)[0]
        return f"{resource}.*" in candidates

    async def _load_permission_map(self, user: "UserContext") -> dict[str, dict[str, Any]]:
        cache_key = f"rbac_perms:{user.user_id}:{user.branch_id or 'none'}"

        # 1) ultra-fast local cache
        cached_local = _LOCAL_CACHE.get(cache_key)
        now = time.time()
        if cached_local and cached_local[0] > now:
            return cached_local[1]

        # 2) redis cache
        try:
            raw = await cache_get(cache_key)
            if raw:
                decoded = orjson.loads(raw)
                _LOCAL_CACHE[cache_key] = (now + _LOCAL_TTL_SECONDS, decoded)
                return decoded
        except Exception:
            pass

        # 3) database
        permission_map: dict[str, dict[str, Any]] = {}
        try:
            async with get_connection() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT bu.role_id, bu.role as branch_role, bu.branch_id,
                           COALESCE(r.name, bu.role) AS role_name, r.branch_id AS role_branch_id
                    FROM branch_users bu
                    LEFT JOIN roles r ON r.id = bu.role_id
                                        WHERE bu.user_id = $1
                                            AND bu.is_active = true
                                            AND ($2::uuid IS NULL OR bu.branch_id = $2::uuid)
                    ORDER BY bu.created_at DESC
                    LIMIT 1
                    """,
                    user.user_id,
                                        user.branch_id,
                )

                if not row:
                    # Owner or non-branch user fallback.
                    role_name = (user.role or "staff").lower()
                    for k in self._fallback_permissions.get(role_name, set()):
                        permission_map[k] = {"allowed": True, "meta": {}}
                    return permission_map

                role_id = row["role_id"]
                role_name = (row["role_name"] or row["branch_role"] or user.role or "staff").lower()

                # Branch isolation: branch users can only use role from their own branch.
                if user.is_branch_user and user.branch_id and str(row["branch_id"]) != str(user.branch_id):
                    logger.warning("rbac_branch_mismatch", user_id=user.user_id, user_branch=user.branch_id, role_branch=row["branch_id"])
                    return {}

                if role_id:
                    rp_rows = await conn.fetch(
                        """
                        SELECT p.key, rp.allowed, rp.meta
                        FROM role_permissions rp
                        JOIN permissions p ON p.id = rp.permission_id
                        WHERE rp.role_id = $1
                        """,
                        role_id,
                    )
                    for rp in rp_rows:
                        permission_map[self._norm(rp["key"])] = {
                            "allowed": bool(rp["allowed"]),
                            "meta": dict(rp["meta"] or {}),
                            "role_id": str(role_id),
                            "role_name": role_name,
                            "branch_id": str(row["branch_id"]) if row["branch_id"] else None,
                        }
                else:
                    for k in self._fallback_permissions.get(role_name, set()):
                        permission_map[k] = {
                            "allowed": True,
                            "meta": {},
                            "role_id": None,
                            "role_name": role_name,
                            "branch_id": str(row["branch_id"]) if row["branch_id"] else None,
                        }

            try:
                await cache_set(cache_key, orjson.dumps(permission_map).decode(), ttl=300)
            except Exception:
                pass
            _LOCAL_CACHE[cache_key] = (now + _LOCAL_TTL_SECONDS, permission_map)
            return permission_map
        except Exception as exc:
            logger.warning("rbac_permission_load_failed", user_id=user.user_id, error=str(exc))
            role_name = (user.role or "staff").lower()
            for k in self._fallback_permissions.get(role_name, set()):
                permission_map[k] = {"allowed": True, "meta": {}}
            return permission_map

    async def invalidate_user_cache(self, user_id: str, branch_id: str | None) -> None:
        cache_key = f"rbac_perms:{user_id}:{branch_id or 'none'}"
        _LOCAL_CACHE.pop(cache_key, None)
        try:
            await cache_delete(cache_key)
        except Exception:
            pass

    async def check_permission(self, user: "UserContext", permission_key: str) -> PermissionDecision:
        permission_map = await self._load_permission_map(user)
        aliases = self._aliases(permission_key)

        for key in aliases:
            details = permission_map.get(key)
            if details and details.get("allowed"):
                return PermissionDecision(
                    permission_key=key,
                    allowed=True,
                    role_id=details.get("role_id") or user.role_id,
                    role_name=details.get("role_name") or user.role,
                    branch_id=details.get("branch_id") or user.branch_id,
                    meta=details.get("meta") or {},
                )
            if self._has_wildcard(key, set(permission_map.keys())):
                details = permission_map.get(key.split(".", 1)[0] + ".*", {})
                return PermissionDecision(
                    permission_key=key,
                    allowed=True,
                    role_id=details.get("role_id") or user.role_id,
                    role_name=details.get("role_name") or user.role,
                    branch_id=details.get("branch_id") or user.branch_id,
                    meta=details.get("meta") or {},
                )

        return PermissionDecision(
            permission_key=self._norm(permission_key),
            allowed=False,
            role_id=user.role_id,
            role_name=user.role,
            branch_id=user.branch_id,
            meta={},
        )


rbac_service = RBACService()
