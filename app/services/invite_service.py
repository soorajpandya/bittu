"""
Staff Invite Service.

Handles:
  1. Creating invites (owner invites staff by email + role)
  2. Listing / revoking invites
  3. Auto-linking: when a user logs in via Google, match their email
     against pending invites and create the branch_user automatically.
"""
import uuid
from typing import Optional

from app.core.database import get_connection, get_serializable_transaction
from app.core.exceptions import (
    NotFoundError, ForbiddenError, ValidationError, ConflictError,
)
from app.core.logging import get_logger
from app.core.redis import cache_delete
from app.services.rbac_service import rbac_service

logger = get_logger(__name__)

VALID_INVITE_ROLES = {"manager", "cashier", "chef", "waiter", "staff"}


class InviteService:

    # ─── Create invite ──────────────────────────────────────────

    async def create_invite(
        self,
        owner_id: str,
        branch_id: str,
        email: str,
        role: str,
    ) -> dict:
        """Owner invites a staff member by email."""
        email = email.strip().lower()
        if not email or "@" not in email:
            raise ValidationError("A valid email address is required")

        if role not in VALID_INVITE_ROLES:
            raise ValidationError(
                f"Invalid role: {role}. Must be one of: {', '.join(sorted(VALID_INVITE_ROLES))}"
            )

        async with get_serializable_transaction() as conn:
            # Verify branch belongs to caller
            branch = await conn.fetchrow(
                "SELECT id, restaurant_id FROM sub_branches WHERE id = $1 AND owner_id = $2",
                branch_id, owner_id,
            )
            if not branch:
                raise NotFoundError("Branch", branch_id)

            # Check if email is already assigned as active staff via existing invites
            # (We can't query auth.users directly; the real duplicate check happens
            # at accept-time when we know the user_id)

            # Resolve role_id
            role_name = "kitchen" if role == "chef" else role
            role_row = await conn.fetchrow(
                "SELECT id FROM roles WHERE branch_id = $1 AND lower(name) = lower($2) LIMIT 1",
                branch_id, role_name,
            )
            role_id = role_row["id"] if role_row else None

            # Upsert: if a revoked/expired invite exists for same email+branch, replace it
            invite_id = str(uuid.uuid4())
            row = await conn.fetchrow(
                """
                INSERT INTO staff_invites (id, branch_id, owner_id, email, role, role_id, status)
                VALUES ($1, $2, $3, $4, $5, $6, 'pending')
                ON CONFLICT (branch_id, lower(email)) WHERE status = 'pending'
                DO UPDATE SET role = EXCLUDED.role,
                              role_id = EXCLUDED.role_id,
                              updated_at = NOW()
                RETURNING id, created_at, expires_at
                """,
                invite_id, branch_id, owner_id, email, role, role_id,
            )

        logger.info("staff_invite_created", email=email, branch_id=branch_id, role=role)
        return {
            "id": str(row["id"]),
            "branch_id": str(branch_id),
            "email": email,
            "role": role,
            "status": "pending",
            "created_at": row["created_at"].isoformat(),
            "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        }

    # ─── List invites ───────────────────────────────────────────

    async def list_invites(
        self,
        owner_id: str,
        branch_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict]:
        """List invites created by this owner, optionally filtered."""
        params: list = [owner_id]
        sql = """
            SELECT i.id, i.branch_id, i.email, i.role, i.status,
                   i.created_at, i.expires_at, i.accepted_at,
                   sb.name as branch_name
            FROM staff_invites i
            JOIN sub_branches sb ON sb.id = i.branch_id
            WHERE i.owner_id = $1
        """
        if branch_id:
            params.append(branch_id)
            sql += f" AND i.branch_id = ${len(params)}"
        if status:
            params.append(status)
            sql += f" AND i.status = ${len(params)}"

        sql += " ORDER BY i.created_at DESC"

        async with get_connection() as conn:
            rows = await conn.fetch(sql, *params)

        return [
            {
                "id": str(r["id"]),
                "branch_id": str(r["branch_id"]),
                "branch_name": r["branch_name"],
                "email": r["email"],
                "role": r["role"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat(),
                "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
                "accepted_at": r["accepted_at"].isoformat() if r["accepted_at"] else None,
            }
            for r in rows
        ]

    # ─── Revoke invite ──────────────────────────────────────────

    async def revoke_invite(self, owner_id: str, invite_id: str) -> dict:
        """Revoke a pending invite."""
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM staff_invites WHERE id = $1 AND owner_id = $2 FOR UPDATE",
                invite_id, owner_id,
            )
            if not row:
                raise NotFoundError("Invite", invite_id)
            if row["status"] != "pending":
                raise ValidationError(f"Cannot revoke invite with status '{row['status']}'")

            await conn.execute(
                "UPDATE staff_invites SET status = 'revoked', updated_at = NOW() WHERE id = $1",
                invite_id,
            )

        logger.info("staff_invite_revoked", invite_id=invite_id)
        return {"id": str(invite_id), "status": "revoked"}

    # ─── Auto-link on login ─────────────────────────────────────

    async def accept_pending_invites(self, user_id: str, email: str) -> list[dict]:
        """
        Called after Google login. If the user's email matches any pending
        invites, create branch_user entries and mark invites accepted.

        Returns list of accepted invites (empty if none matched).
        """
        if not email:
            return []

        email = email.strip().lower()
        accepted = []

        async with get_serializable_transaction() as conn:
            # Find all pending, non-expired invites for this email
            invites = await conn.fetch(
                """
                SELECT id, branch_id, owner_id, role, role_id
                FROM staff_invites
                WHERE lower(email) = $1
                  AND status = 'pending'
                  AND (expires_at IS NULL OR expires_at > NOW())
                FOR UPDATE
                """,
                email,
            )

            if not invites:
                return []

            for inv in invites:
                branch_id = str(inv["branch_id"])
                owner_id = inv["owner_id"]
                role = inv["role"]
                role_id = inv["role_id"]

                # Skip if user is already active on this branch
                existing = await conn.fetchrow(
                    "SELECT 1 FROM branch_users WHERE user_id = $1 AND branch_id = $2 AND is_active = true",
                    user_id, branch_id,
                )
                if existing:
                    # Mark invite accepted anyway (user already linked)
                    await conn.execute(
                        "UPDATE staff_invites SET status = 'accepted', accepted_at = NOW(), updated_at = NOW() WHERE id = $1",
                        inv["id"],
                    )
                    continue

                # Deactivate any existing branch_user for this user (they're switching)
                await conn.execute(
                    "UPDATE branch_users SET is_active = false WHERE user_id = $1 AND is_active = true",
                    user_id,
                )

                # Create branch_user
                await conn.execute(
                    """
                    INSERT INTO branch_users (user_id, branch_id, owner_id, role, role_id, is_active)
                    VALUES ($1, $2, $3, $4, $5, true)
                    ON CONFLICT (user_id, branch_id) DO UPDATE
                        SET role = EXCLUDED.role,
                            role_id = EXCLUDED.role_id,
                            owner_id = EXCLUDED.owner_id,
                            is_active = true
                    """,
                    user_id, branch_id, owner_id, role, role_id,
                )

                # Mark invite accepted
                await conn.execute(
                    "UPDATE staff_invites SET status = 'accepted', accepted_at = NOW(), updated_at = NOW() WHERE id = $1",
                    inv["id"],
                )

                accepted.append({
                    "invite_id": str(inv["id"]),
                    "branch_id": branch_id,
                    "role": role,
                })

                logger.info(
                    "staff_invite_auto_accepted",
                    user_id=user_id,
                    email=email,
                    branch_id=branch_id,
                    role=role,
                )

        # Invalidate auth cache so next request picks up the new role
        if accepted:
            try:
                await cache_delete(f"user_ctx:{user_id}")
            except Exception:
                pass
            await rbac_service.invalidate_user_cache(user_id=user_id, branch_id=None)

        return accepted


invite_service = InviteService()
