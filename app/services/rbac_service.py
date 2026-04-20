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

# ── Observability counters (in-memory, per-worker) ──
_FALLBACK_COUNTER: dict[str, list[float]] = {}   # reason -> [timestamps]
_DENIAL_COUNTER: dict[str, list[float]] = {}      # user_id -> [timestamps]
_ANOMALY_WINDOW = 300   # 5-minute sliding window
_FALLBACK_WARN_THRESHOLD = 10   # warn if >10 fallbacks in 5min
_DENIAL_WARN_THRESHOLD = 5      # warn if same user denied >5 times in 5min


class RBACService:
    def __init__(self) -> None:
        # Fallback permissions: key -> meta dict.  Used when DB is unavailable.
        self._fallback_permissions: dict[str, dict[str, dict]] = {
            "owner": {
                "order.*": {}, "orders.*": {}, "billing.*": {}, "payment.*": {}, "payments.*": {},
                "table.*": {}, "tables.*": {}, "inventory.*": {}, "voice.use": {}, "kitchen.*": {},
                "kitchen_station.*": {},
                # Staff & branch management
                "staff.branches.read": {}, "staff.branches.create": {}, "staff.branches.update": {},
                "staff.read": {}, "staff.create": {}, "staff.update": {}, "staff.delete": {},
                "staff.branch_users.read": {}, "staff.branch_users.create": {},
                "staff.branch_users.update": {}, "staff.branch_users.delete": {},
                # Accounting
                "accounting.read": {}, "accounting.write": {},
                # ERP
                "erp.read": {}, "erp.write": {}, "erp.shifts.read": {},
                "erp.shifts.manage": {}, "erp.seed": {},
                # Subscriptions & Billing
                "subscription.read": {}, "subscription.write": {}, "subscription.admin": {},
                "billing.read": {},
                # Cash Transactions
                "cash_transaction.read": {}, "cash_transaction.create": {}, "cash_transaction.delete": {},
                # Menu
                "menu.read": {}, "menu.write": {}, "menu.delete": {},
                # Analytics
                "analytics.read": {},
                # Waitlist
                "waitlist.read": {}, "waitlist.manage": {}, "waitlist.admin": {},
                # Dinein
                "dinein.manage": {},
                # Customers
                "customer.read": {}, "customer.write": {}, "customer.delete": {},
                # Promotions
                "promotion.read": {}, "promotion.write": {}, "promotion.delete": {},
                # Due Payments
                "due_payment.read": {}, "due_payment.write": {}, "due_payment.delete": {},
                # Feedback
                "feedback.read": {}, "feedback.write": {}, "feedback.delete": {},
                # Settings
                "settings.read": {}, "settings.admin": {},
                # Purchase Orders
                "purchase_order.read": {}, "purchase_order.write": {}, "purchase_order.delete": {},
                # Audit
                "audit.read": {},
                # Delivery (pincodes)
                "delivery.read": {}, "delivery.write": {}, "delivery.delete": {},
                # Favourites
                "favourites.manage": {},
            },
            "manager": {
                "order.create": {}, "order.edit": {}, "order.cancel": {}, "order.read": {},
                "orders.create": {}, "orders.read": {}, "orders.update": {},
                "billing.generate": {}, "billing.discount": {"max_discount_percent": 50},
                "payment.create": {}, "payments.create": {},
                "payment.refund": {"max_refund_amount": 5000},
                "table.read": {}, "table.start": {}, "table.close": {}, "table.manage": {}, "tables.manage": {},
                "inventory.read": {}, "inventory.update": {}, "inventory.manage": {},
                "kitchen.read": {}, "kitchen.update": {},
                "kitchen_station.read": {}, "kitchen_station.manage": {},
                # Staff (read-only) + accounting
                "staff.read": {}, "staff.branch_users.read": {},
                "accounting.read": {}, "accounting.write": {},
                # ERP
                "erp.read": {}, "erp.write": {}, "erp.shifts.read": {}, "erp.shifts.manage": {},
                # Cash Transactions
                "cash_transaction.read": {}, "cash_transaction.create": {}, "cash_transaction.delete": {},
                # Menu
                "menu.read": {}, "menu.write": {},
                # Analytics
                "analytics.read": {},
                # Waitlist
                "waitlist.read": {}, "waitlist.manage": {}, "waitlist.admin": {},
                # Dinein
                "dinein.manage": {},
                # Customers
                "customer.read": {}, "customer.write": {},
                # Promotions
                "promotion.read": {}, "promotion.write": {},
                # Due Payments
                "due_payment.read": {}, "due_payment.write": {}, "due_payment.delete": {},
                # Feedback
                "feedback.read": {}, "feedback.write": {},
                # Settings
                "settings.read": {},
                # Purchase Orders
                "purchase_order.read": {}, "purchase_order.write": {},
                # Delivery
                "delivery.read": {}, "delivery.write": {},
                # Favourites
                "favourites.manage": {},
            },
            "cashier": {
                "order.read": {}, "order.edit": {}, "orders.read": {}, "orders.update": {},
                "billing.generate": {}, "billing.discount": {"max_discount_percent": 10},
                "payment.create": {}, "payments.create": {},
                "table.read": {}, "table.start": {}, "table.close": {}, "table.manage": {}, "tables.manage": {},
                # ERP shifts & Cash Transactions
                "erp.shifts.read": {}, "erp.shifts.manage": {},
                "cash_transaction.read": {}, "cash_transaction.create": {},
                # Menu (read-only)
                "menu.read": {},
                # Waitlist
                "waitlist.read": {}, "waitlist.manage": {},
                # Customers
                "customer.read": {}, "customer.write": {},
                # Due Payments
                "due_payment.read": {}, "due_payment.write": {},
                # Feedback
                "feedback.write": {},
                # Favourites
                "favourites.manage": {},
            },
            "waiter": {
                "order.create": {}, "order.read": {}, "orders.create": {}, "orders.read": {},
                "table.read": {}, "table.start": {}, "table.close": {}, "table.manage": {}, "tables.manage": {},
                "kitchen.read": {},
                # Waitlist
                "waitlist.read": {}, "waitlist.manage": {},
                # Dinein
                "dinein.manage": {},
                # Feedback
                "feedback.write": {},
                # Favourites
                "favourites.manage": {},
            },
            "chef": {"order.read": {}, "orders.read": {}, "kitchen.read": {}, "kitchen.update": {}, "kitchen_station.read": {}},
            "kitchen": {"order.read": {}, "orders.read": {}, "kitchen.read": {}, "kitchen.update": {}, "kitchen_station.read": {}},
            "staff": {
                "order.read": {}, "orders.read": {}, "table.read": {}, "kitchen.read": {},
                # Waitlist (read)
                "waitlist.read": {},
            },
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
                    logger.info("rbac_fallback_used", user_id=user.user_id, role=role_name, reason="no_branch_user_row")
                    self._track_fallback("no_branch_user_row")
                    for k, meta in self._fallback_permissions.get(role_name, {}).items():
                        permission_map[k] = {"allowed": True, "meta": meta}
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
                    logger.info("rbac_fallback_used", user_id=user.user_id, role=role_name, reason="no_role_id_assigned")
                    self._track_fallback("no_role_id_assigned")
                    for k, meta in self._fallback_permissions.get(role_name, {}).items():
                        permission_map[k] = {
                            "allowed": True,
                            "meta": meta,
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
            logger.info("rbac_fallback_used", user_id=user.user_id, role=role_name, reason="db_error", error=str(exc))
            self._track_fallback("db_error")
            for k, meta in self._fallback_permissions.get(role_name, {}).items():
                permission_map[k] = {"allowed": True, "meta": meta}
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

        norm_key = self._norm(permission_key)
        logger.info(
            "rbac_permission_denied",
            user_id=user.user_id,
            role=user.role,
            permission=norm_key,
            branch_id=str(user.branch_id) if user.branch_id else None,
        )
        self._track_denial(user.user_id, norm_key)
        return PermissionDecision(
            permission_key=norm_key,
            allowed=False,
            role_id=user.role_id,
            role_name=user.role,
            branch_id=user.branch_id,
            meta={},
        )

    # ── Anomaly tracking helpers ──

    @staticmethod
    def _track_fallback(reason: str) -> None:
        now = time.time()
        ts_list = _FALLBACK_COUNTER.setdefault(reason, [])
        ts_list.append(now)
        cutoff = now - _ANOMALY_WINDOW
        _FALLBACK_COUNTER[reason] = [t for t in ts_list if t > cutoff]
        if len(_FALLBACK_COUNTER[reason]) >= _FALLBACK_WARN_THRESHOLD:
            logger.warning(
                "rbac_fallback_spike",
                reason=reason,
                count=len(_FALLBACK_COUNTER[reason]),
                window_seconds=_ANOMALY_WINDOW,
            )

    @staticmethod
    def _track_denial(user_id: str, permission: str) -> None:
        now = time.time()
        key = f"{user_id}:{permission}"
        ts_list = _DENIAL_COUNTER.setdefault(key, [])
        ts_list.append(now)
        cutoff = now - _ANOMALY_WINDOW
        _DENIAL_COUNTER[key] = [t for t in ts_list if t > cutoff]
        if len(_DENIAL_COUNTER[key]) >= _DENIAL_WARN_THRESHOLD:
            logger.warning(
                "rbac_repeated_denial",
                user_id=user_id,
                permission=permission,
                count=len(_DENIAL_COUNTER[key]),
                window_seconds=_ANOMALY_WINDOW,
            )

    async def get_user_permissions(self, user: "UserContext") -> dict[str, Any]:
        """Return the full permission map for the user (for /auth/permissions/me)."""
        permission_map = await self._load_permission_map(user)
        source = "db"

        # Determine source: if any entry lacks role_id, we're on fallback
        if permission_map:
            sample = next(iter(permission_map.values()))
            if sample.get("role_id") is None and not sample.get("branch_id"):
                source = "fallback"
                logger.info("permissions_served_from_fallback", user_id=user.user_id, role=user.role)

        # Consistent shape: every permission is {"allowed": bool, "meta": dict}
        permissions: dict[str, dict[str, Any]] = {}
        last_details: dict[str, Any] = {}
        for key, details in permission_map.items():
            last_details = details
            permissions[key] = {
                "allowed": details.get("allowed", False),
                "meta": details.get("meta") or {},
            }

        return {
            "role": last_details.get("role_name") or user.role if permission_map else user.role,
            "role_id": last_details.get("role_id") or user.role_id if permission_map else user.role_id,
            "branch_id": str(user.branch_id) if user.branch_id else None,
            "source": source,
            "permissions": permissions,
        }


rbac_service = RBACService()
