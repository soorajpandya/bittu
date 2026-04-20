"""Staff, Branch & RBAC endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, EmailStr

from app.core.auth import UserContext, get_current_user, require_permission
from app.core.database import get_connection
from app.services.staff_service import StaffService
from app.services.invite_service import invite_service

router = APIRouter(prefix="/staff", tags=["Staff"])
_svc = StaffService()


# ─── Request models ────────────────────────────────────────

class CreateBranchIn(BaseModel):
    name: str
    manager_user_id: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None


class UpdateBranchIn(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None


class AddBranchUserIn(BaseModel):
    branch_id: str
    user_id: str
    role: str = "manager"


class UpdateBranchUserIn(BaseModel):
    role: str


class CreateStaffIn(BaseModel):
    branch_id: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    role: str = "staff"
    phone: Optional[str] = None


class UpdateStaffIn(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    phone: Optional[str] = None


# ─── Branch endpoints ──────────────────────────────────────

@router.get("/branches")
async def list_branches(user: UserContext = Depends(require_permission("staff.branches.read"))):
    """Get all branches for the current owner."""
    return await _svc.get_branches(user=user)


@router.post("/branches", status_code=201)
async def create_branch(
    body: CreateBranchIn,
    user: UserContext = Depends(require_permission("staff.branches.create")),
):
    """Create a new sub-branch. Optionally assign a manager by Supabase user_id."""
    return await _svc.create_sub_branch(
        user=user,
        name=body.name,
        manager_user_id=body.manager_user_id,
        address=body.address,
        phone=body.phone,
    )


@router.patch("/branches/{branch_id}")
async def update_branch(
    branch_id: str,
    body: UpdateBranchIn,
    user: UserContext = Depends(require_permission("staff.branches.update")),
):
    return await _svc.update_branch(
        user=user,
        branch_id=branch_id,
        **body.model_dump(exclude_none=True),
    )


# ─── Branch user endpoints (login-capable) ─────────────────

@router.get("/branch-users/me")
async def get_my_branch_user(
    user: UserContext = Depends(get_current_user),
):
    """Get the current user's branch_user record (role, branch, etc.)."""
    return await _svc.get_my_branch_user(user=user)


@router.get("/branch-users")
async def list_branch_users(
    branch_id: Optional[str] = None,
    user: UserContext = Depends(require_permission("staff.branch_users.read")),
):
    """List all branch users (managers, cashiers, etc.) that can log in."""
    return await _svc.list_branch_users(user=user, branch_id=branch_id)


@router.post("/branch-users", status_code=201)
async def add_branch_user(
    body: AddBranchUserIn,
    user: UserContext = Depends(require_permission("staff.branch_users.create")),
):
    """Assign a Supabase user to a branch with a role (e.g. manager)."""
    return await _svc.add_branch_user(
        user=user,
        branch_id=body.branch_id,
        target_user_id=body.user_id,
        role=body.role,
    )


@router.patch("/branch-users/{target_user_id}")
async def update_branch_user_role(
    target_user_id: str,
    body: UpdateBranchUserIn,
    user: UserContext = Depends(require_permission("staff.branch_users.update")),
):
    """Change a branch user's role."""
    return await _svc.update_branch_user_role(
        user=user,
        target_user_id=target_user_id,
        role=body.role,
    )


@router.delete("/branch-users/{target_user_id}")
async def remove_branch_user(
    target_user_id: str,
    user: UserContext = Depends(require_permission("staff.branch_users.delete")),
):
    """Deactivate a branch user (they lose login access to that branch)."""
    return await _svc.remove_branch_user(user=user, target_user_id=target_user_id)


# ─── Local staff records (no login) ────────────────────────

@router.post("")
async def create_staff(
    body: CreateStaffIn,
    user: UserContext = Depends(require_permission("staff.create")),
):
    branch_id = body.branch_id
    if not branch_id:
        # Auto-resolve main branch for the owner
        if user.branch_id:
            branch_id = user.branch_id
        else:
            async with get_connection() as conn:
                row = await conn.fetchrow(
                    "SELECT id FROM sub_branches WHERE owner_id = $1 AND is_main_branch = true LIMIT 1",
                    user.user_id,
                )
                if row:
                    branch_id = str(row["id"])
    if not branch_id:
        from app.core.exceptions import ValidationError
        raise ValidationError("No branch found. Please create a restaurant first.")

    # Default email/name from auth context if not provided
    email = body.email or user.email or ""
    name = body.name or (email.split("@")[0] if email else "Staff")

    return await _svc.create_branch_user(
        user=user,
        branch_id=branch_id,
        email=email,
        name=name,
        role=body.role,
        phone=body.phone,
    )


@router.get("")
async def list_staff(
    branch_id: Optional[str] = None,
    user: UserContext = Depends(require_permission("staff.read")),
):
    return await _svc.get_branch_users(user=user, branch_id=branch_id)


@router.patch("/{staff_id}")
async def update_staff(
    staff_id: str,
    body: UpdateStaffIn,
    user: UserContext = Depends(require_permission("staff.update")),
):
    return await _svc.update_branch_user(
        user=user,
        branch_user_id=staff_id,
        **body.model_dump(exclude_none=True),
    )


@router.delete("/{staff_id}")
async def deactivate_staff(
    staff_id: str,
    user: UserContext = Depends(require_permission("staff.delete")),
):
    return await _svc.deactivate_branch_user(user=user, branch_user_id=staff_id)


# ─── Staff invite endpoints ────────────────────────────────

class CreateInviteIn(BaseModel):
    branch_id: str
    email: EmailStr
    role: str = "staff"


@router.post("/invites", status_code=201)
async def create_invite(
    body: CreateInviteIn,
    user: UserContext = Depends(require_permission("staff.invites.create")),
):
    """Invite a staff member by email. They will be auto-linked on Google login."""
    return await invite_service.create_invite(
        owner_id=user.user_id,
        branch_id=body.branch_id,
        email=body.email,
        role=body.role,
    )


@router.get("/invites")
async def list_invites(
    branch_id: Optional[str] = None,
    status: Optional[str] = Query(None, pattern="^(pending|accepted|revoked|expired)$"),
    user: UserContext = Depends(require_permission("staff.invites.read")),
):
    """List staff invites, optionally filtered by branch and status."""
    return await invite_service.list_invites(
        owner_id=user.user_id,
        branch_id=branch_id,
        status=status,
    )


@router.delete("/invites/{invite_id}")
async def revoke_invite(
    invite_id: str,
    user: UserContext = Depends(require_permission("staff.invites.revoke")),
):
    """Revoke a pending invite."""
    return await invite_service.revoke_invite(
        owner_id=user.user_id,
        invite_id=invite_id,
    )
