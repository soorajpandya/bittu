"""
Staff Service.

Manages:
  1. Sub-branch creation — owner creates branches under their restaurant;
     auto-creates a manager entry in `branch_users` so the manager can log in.
  2. Branch users (login-capable) — entries in `branch_users` table that
     the auth resolver uses to grant role-scoped access.
  3. Local staff records — lightweight entries in `staff` table for
     personnel tracking (no Supabase account required).
"""
import uuid
from typing import Optional

from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.exceptions import NotFoundError, ForbiddenError, ValidationError, ConflictError
from app.core.logging import get_logger
from app.core.redis import cache_delete
from app.services.rbac_service import rbac_service

logger = get_logger(__name__)

VALID_STAFF_ROLES = {"manager", "cashier", "chef", "waiter", "staff"}
VALID_BRANCH_USER_ROLES = {"manager", "cashier", "chef", "waiter", "staff"}


class StaffService:

    async def _resolve_role_id(self, conn, branch_id: str, role: str) -> Optional[str]:
        """Resolve role_id for a branch role using RBAC table naming."""
        role_name = "kitchen" if role == "chef" else role
        role_row = await conn.fetchrow(
            "SELECT id FROM roles WHERE branch_id = $1 AND lower(name) = lower($2) LIMIT 1",
            branch_id,
            role_name,
        )
        return str(role_row["id"]) if role_row else None

    async def _invalidate_auth_cache(self, user_id: str, branch_id: Optional[str]) -> None:
        try:
            await cache_delete(f"user_ctx:{user_id}")
        except Exception:
            pass
        await rbac_service.invalidate_user_cache(user_id=user_id, branch_id=branch_id)

    # ─── Sub-branch management ──────────────────────────────────

    async def create_sub_branch(
        self,
        user: UserContext,
        name: str,
        manager_user_id: Optional[str] = None,
        address: Optional[str] = None,
        phone: Optional[str] = None,
    ) -> dict:
        """
        Create a new sub-branch under the owner's restaurant.
        If manager_user_id is supplied, an entry is inserted into
        `branch_users` so that user resolves as manager of this branch
        on login.
        """
        if user.role != "owner":
            raise ForbiddenError("Only owners can create branches")

        branch_id = str(uuid.uuid4())

        async with get_serializable_transaction() as conn:
            # Get the owner's restaurant
            restaurant = await conn.fetchrow(
                "SELECT id FROM restaurants WHERE owner_id = $1",
                user.user_id,
            )
            if not restaurant:
                raise NotFoundError("Restaurant", user.user_id)

            restaurant_id = str(restaurant["id"])

            await conn.execute(
                """
                INSERT INTO sub_branches (id, restaurant_id, owner_id, name, is_main_branch, is_active, created_at)
                VALUES ($1, $2, $3, $4, false, true, NOW())
                """,
                branch_id, restaurant_id, user.user_id, name,
            )

            manager_info = None
            if manager_user_id:
                # Prevent assigning the owner as a branch user
                if manager_user_id == user.user_id:
                    raise ValidationError("Owner cannot be assigned as a branch user")

                # Check if this user is already a branch user elsewhere
                existing = await conn.fetchrow(
                    "SELECT branch_id FROM branch_users WHERE user_id = $1 AND is_active = true",
                    manager_user_id,
                )
                if existing:
                    raise ConflictError(
                        f"User {manager_user_id} is already assigned to branch {existing['branch_id']}"
                    )

                await conn.execute(
                    """
                    INSERT INTO branch_users (user_id, branch_id, owner_id, role, role_id, is_active)
                    VALUES ($1, $2, $3, 'manager',
                            (SELECT id FROM roles WHERE branch_id = $2 AND lower(name) = 'manager' LIMIT 1),
                            true)
                    """,
                    manager_user_id, branch_id, user.user_id,
                )
                manager_info = {
                    "user_id": manager_user_id,
                    "role": "manager",
                }

        logger.info(
            "sub_branch_created",
            branch_id=branch_id,
            restaurant_id=restaurant_id,
            manager_user_id=manager_user_id,
        )
        result = {
            "id": branch_id,
            "restaurant_id": restaurant_id,
            "name": name,
            "is_main_branch": False,
            "is_active": True,
        }
        if manager_info:
            result["manager"] = manager_info
        return result

    async def update_branch(
        self,
        user: UserContext,
        branch_id: str,
        name: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> dict:
        """Update a sub-branch's name or active status."""
        if user.role != "owner":
            raise ForbiddenError("Only owners can update branches")

        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sub_branches WHERE id = $1 AND owner_id = $2 FOR UPDATE",
                branch_id, user.user_id,
            )
            if not row:
                raise NotFoundError("Branch", branch_id)

            updates = {}
            if name is not None:
                updates["name"] = name
            if is_active is not None:
                if row["is_main_branch"] and not is_active:
                    raise ValidationError("Cannot deactivate the main branch")
                updates["is_active"] = is_active

            if not updates:
                return dict(row)

            set_parts = []
            vals = [branch_id, user.user_id]
            for k, v in updates.items():
                vals.append(v)
                set_parts.append(f"{k} = ${len(vals)}")

            updated = await conn.fetchrow(
                f"UPDATE sub_branches SET {', '.join(set_parts)} WHERE id = $1 AND owner_id = $2 RETURNING *",
                *vals,
            )
        return dict(updated)

    # ─── Branch users (login-capable) ───────────────────────────

    async def add_branch_user(
        self,
        user: UserContext,
        branch_id: str,
        target_user_id: str,
        role: str = "manager",
    ) -> dict:
        """
        Add a Supabase user to a branch so they can log in and be
        resolved as a branch user by the auth system.
        """
        if user.role != "owner":
            raise ForbiddenError("Only owners can assign branch users")

        if role not in VALID_BRANCH_USER_ROLES:
            raise ValidationError(
                f"Invalid role: {role}. Must be one of: {', '.join(VALID_BRANCH_USER_ROLES)}"
            )

        if target_user_id == user.user_id:
            raise ValidationError("Owner cannot be assigned as a branch user")

        async with get_serializable_transaction() as conn:
            # Verify branch belongs to owner
            branch = await conn.fetchrow(
                "SELECT id, restaurant_id FROM sub_branches WHERE id = $1 AND owner_id = $2",
                branch_id, user.user_id,
            )
            if not branch:
                raise NotFoundError("Branch", branch_id)

            # Check if already assigned
            existing = await conn.fetchrow(
                "SELECT user_id, branch_id, is_active FROM branch_users WHERE user_id = $1",
                target_user_id,
            )
            if existing and existing["is_active"]:
                raise ConflictError(
                    f"User is already assigned to branch {existing['branch_id']}"
                )

            if existing:
                role_id = await self._resolve_role_id(conn, branch_id=branch_id, role=role)
                # Reactivate and reassign
                await conn.execute(
                    """
                    UPDATE branch_users
                    SET branch_id = $1, role = $2, role_id = $3, owner_id = $4, is_active = true
                    WHERE user_id = $5
                    """,
                    branch_id, role, role_id, user.user_id, target_user_id,
                )
            else:
                role_id = await self._resolve_role_id(conn, branch_id=branch_id, role=role)
                await conn.execute(
                    """
                    INSERT INTO branch_users (user_id, branch_id, owner_id, role, role_id, is_active)
                    VALUES ($1, $2, $3, $4, $5, true)
                    """,
                    target_user_id, branch_id, user.user_id, role, role_id,
                )

        await self._invalidate_auth_cache(user_id=target_user_id, branch_id=branch_id)

        logger.info(
            "branch_user_added",
            target_user_id=target_user_id,
            branch_id=branch_id,
            role=role,
        )
        return {
            "user_id": target_user_id,
            "branch_id": branch_id,
            "owner_id": user.user_id,
            "role": role,
            "is_active": True,
        }

    async def get_my_branch_user(self, user: UserContext) -> dict:
        """Get the current user's branch_user record."""
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT bu.user_id, bu.branch_id, bu.owner_id, bu.role, bu.is_active,
                       sb.name as branch_name, r.name as restaurant_name, r.id as restaurant_id
                FROM branch_users bu
                JOIN sub_branches sb ON sb.id = bu.branch_id
                JOIN restaurants r ON r.id = sb.restaurant_id
                WHERE bu.user_id = $1 AND bu.is_active = true
                LIMIT 1
                """,
                user.user_id,
            )
            if not row:
                # Check if user is an owner directly
                owner_row = await conn.fetchrow(
                    """
                    SELECT r.id as restaurant_id, r.name as restaurant_name,
                           sb.id as branch_id, sb.name as branch_name
                    FROM restaurants r
                    LEFT JOIN sub_branches sb ON sb.restaurant_id = r.id AND sb.is_main_branch = true
                    WHERE r.owner_id = $1
                    LIMIT 1
                    """,
                    user.user_id,
                )
                if owner_row:
                    return {
                        "user_id": user.user_id,
                        "branch_id": str(owner_row["branch_id"]) if owner_row["branch_id"] else None,
                        "owner_id": user.user_id,
                        "role": "owner",
                        "is_active": True,
                        "branch_name": owner_row["branch_name"],
                        "restaurant_name": owner_row["restaurant_name"],
                        "restaurant_id": str(owner_row["restaurant_id"]),
                    }
                raise NotFoundError("No branch user record found for current user")
            return dict(row)

    async def list_branch_users(
        self,
        user: UserContext,
        branch_id: Optional[str] = None,
    ) -> list[dict]:
        """List all branch_users (login-capable) for the owner, optionally filtered by branch."""
        if user.role != "owner":
            raise ForbiddenError("Only owners can view branch users")

        params = [user.user_id]
        sql = """
            SELECT bu.user_id, bu.branch_id, bu.owner_id, bu.role, bu.is_active,
                   sb.name as branch_name
            FROM branch_users bu
            JOIN sub_branches sb ON sb.id = bu.branch_id
            WHERE bu.owner_id = $1
        """
        if branch_id:
            params.append(branch_id)
            sql += f" AND bu.branch_id = ${len(params)}"

        sql += " ORDER BY sb.name, bu.role"

        async with get_connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    async def update_branch_user_role(
        self,
        user: UserContext,
        target_user_id: str,
        role: str,
    ) -> dict:
        """Change the role of an existing branch user."""
        if user.role != "owner":
            raise ForbiddenError("Only owners can modify branch users")

        if role not in VALID_BRANCH_USER_ROLES:
            raise ValidationError(
                f"Invalid role: {role}. Must be one of: {', '.join(VALID_BRANCH_USER_ROLES)}"
            )

        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM branch_users WHERE user_id = $1 AND owner_id = $2 FOR UPDATE",
                target_user_id, user.user_id,
            )
            if not row:
                raise NotFoundError("BranchUser", target_user_id)

            await conn.execute(
                """
                UPDATE branch_users bu
                SET role = $1,
                    role_id = (
                        SELECT id FROM roles
                        WHERE branch_id = bu.branch_id
                          AND lower(name) = lower($2)
                        LIMIT 1
                    )
                WHERE user_id = $3
                """,
                role,
                "kitchen" if role == "chef" else role,
                target_user_id,
            )

        await self._invalidate_auth_cache(user_id=target_user_id, branch_id=str(row["branch_id"]))

        logger.info("branch_user_role_updated", target_user_id=target_user_id, role=role)
        return {
            "user_id": target_user_id,
            "branch_id": str(row["branch_id"]),
            "role": role,
            "is_active": row["is_active"],
        }

    async def remove_branch_user(
        self,
        user: UserContext,
        target_user_id: str,
    ) -> dict:
        """Deactivate a branch user (they can no longer log in as branch user)."""
        if user.role != "owner":
            raise ForbiddenError("Only owners can remove branch users")

        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM branch_users WHERE user_id = $1 AND owner_id = $2",
                target_user_id, user.user_id,
            )
            if not row:
                raise NotFoundError("BranchUser", target_user_id)

            await conn.execute(
                "UPDATE branch_users SET is_active = false WHERE user_id = $1",
                target_user_id,
            )

        await self._invalidate_auth_cache(user_id=target_user_id, branch_id=str(row["branch_id"]))

        logger.info("branch_user_removed", target_user_id=target_user_id)
        return {"user_id": target_user_id, "is_active": False}

    # ─── Local staff records (no login) ─────────────────────────

    async def create_branch_user(
        self,
        user: UserContext,
        branch_id: str,
        email: str,
        name: str,
        role: str = "staff",
        phone: Optional[str] = None,
    ) -> dict:
        """Create a staff record under a branch owned by the current user."""
        if user.role != "owner":
            raise ForbiddenError("Only owners can create staff")

        if role not in VALID_STAFF_ROLES:
            raise ValidationError(
                f"Invalid role: {role}. Must be one of: {', '.join(VALID_STAFF_ROLES)}"
            )

        async with get_serializable_transaction() as conn:
            # Verify branch belongs to owner
            branch = await conn.fetchrow(
                "SELECT id, restaurant_id FROM sub_branches WHERE id = $1 AND owner_id = $2",
                branch_id, user.user_id,
            )
            if not branch:
                raise NotFoundError("Branch", branch_id)

            row = await conn.fetchrow(
                """
                INSERT INTO staff (restaurant_id, branch_id, name, phone, role, is_active)
                VALUES ($1, $2, $3, $4, $5, true)
                RETURNING id, created_at
                """,
                branch["restaurant_id"], branch_id, name, phone, role,
            )

        logger.info("staff_created", branch_id=branch_id, name=name, role=role)
        return {
            "id": str(row["id"]),
            "restaurant_id": str(branch["restaurant_id"]),
            "branch_id": branch_id,
            "name": name,
            "phone": phone,
            "role": role,
            "is_active": True,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }

    async def update_branch_user(
        self,
        user: UserContext,
        branch_user_id: str,
        name: Optional[str] = None,
        role: Optional[str] = None,
        phone: Optional[str] = None,
    ) -> dict:
        """Update a staff member's details."""
        if user.role != "owner":
            raise ForbiddenError("Only owners can modify staff")

        if role is not None and role not in VALID_STAFF_ROLES:
            raise ValidationError(
                f"Invalid role: {role}. Must be one of: {', '.join(VALID_STAFF_ROLES)}"
            )

        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                SELECT s.* FROM staff s
                JOIN sub_branches sb ON sb.id = s.branch_id
                WHERE s.id = $1 AND sb.owner_id = $2
                FOR UPDATE OF s
                """,
                branch_user_id, user.user_id,
            )
            if not row:
                raise NotFoundError("Staff", branch_user_id)

            updates = {}
            if name is not None:
                updates["name"] = name
            if role is not None:
                updates["role"] = role
            if phone is not None:
                updates["phone"] = phone

            if not updates:
                return dict(row)

            set_clauses = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(updates.keys()))
            values = [branch_user_id] + list(updates.values())

            await conn.execute(
                f"UPDATE staff SET {set_clauses} WHERE id = $1",
                *values,
            )

        logger.info("staff_updated", staff_id=branch_user_id, updates=updates)
        return {"id": branch_user_id, **updates}

    async def get_branch_users(
        self,
        user: UserContext,
        branch_id: Optional[str] = None,
    ) -> list[dict]:
        """Get all staff for the owner, optionally filtered by branch."""
        if user.role != "owner":
            raise ForbiddenError("Only owners can view staff")

        params = [user.user_id]
        query = """
            SELECT s.*, sb.name as branch_name
            FROM staff s
            JOIN sub_branches sb ON sb.id = s.branch_id
            WHERE sb.owner_id = $1
        """
        if branch_id:
            params.append(branch_id)
            query += f" AND s.branch_id = ${len(params)}"

        query += " ORDER BY s.created_at DESC"

        async with get_connection() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    async def deactivate_branch_user(
        self,
        user: UserContext,
        branch_user_id: str,
    ) -> dict:
        """Deactivate a staff member (soft delete)."""
        if user.role != "owner":
            raise ForbiddenError("Only owners can modify staff")

        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                SELECT s.id FROM staff s
                JOIN sub_branches sb ON sb.id = s.branch_id
                WHERE s.id = $1 AND sb.owner_id = $2
                """,
                branch_user_id, user.user_id,
            )
            if not row:
                raise NotFoundError("Staff", branch_user_id)

            await conn.execute(
                "UPDATE staff SET is_active = false WHERE id = $1",
                branch_user_id,
            )

        logger.info("staff_deactivated", staff_id=branch_user_id)
        return {"id": branch_user_id, "is_active": False}

    async def get_branches(self, user: UserContext) -> list[dict]:
        """Get all branches for the owner."""
        if user.role != "owner":
            raise ForbiddenError("Only owners can view branches")

        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT sb.*, r.name as restaurant_name
                FROM sub_branches sb
                JOIN restaurants r ON r.id = sb.restaurant_id
                WHERE sb.owner_id = $1
                ORDER BY sb.is_main_branch DESC, sb.created_at DESC
                """,
                user.user_id,
            )
            return [dict(r) for r in rows]
