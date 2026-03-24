"""Staff, Branch & RBAC endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, require_role
from app.services.staff_service import StaffService

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
    branch_id: str
    email: str
    name: str
    role: str
    phone: Optional[str] = None


class UpdateStaffIn(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    phone: Optional[str] = None


# ─── Branch endpoints ──────────────────────────────────────

@router.get("/branches")
async def list_branches(user: UserContext = Depends(require_role("owner"))):
    """Get all branches for the current owner."""
    return await _svc.get_branches(user=user)


@router.post("/branches", status_code=201)
async def create_branch(
    body: CreateBranchIn,
    user: UserContext = Depends(require_role("owner")),
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
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.update_branch(
        user=user,
        branch_id=branch_id,
        **body.model_dump(exclude_none=True),
    )


# ─── Branch user endpoints (login-capable) ─────────────────

@router.get("/branch-users/me")
async def get_my_branch_user(
    user: UserContext = Depends(require_role("owner", "manager", "cashier", "chef", "waiter", "staff")),
):
    """Get the current user's branch_user record (role, branch, etc.)."""
    return await _svc.get_my_branch_user(user=user)


@router.get("/branch-users")
async def list_branch_users(
    branch_id: Optional[str] = None,
    user: UserContext = Depends(require_role("owner")),
):
    """List all branch users (managers, cashiers, etc.) that can log in."""
    return await _svc.list_branch_users(user=user, branch_id=branch_id)


@router.post("/branch-users", status_code=201)
async def add_branch_user(
    body: AddBranchUserIn,
    user: UserContext = Depends(require_role("owner")),
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
    user: UserContext = Depends(require_role("owner")),
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
    user: UserContext = Depends(require_role("owner")),
):
    """Deactivate a branch user (they lose login access to that branch)."""
    return await _svc.remove_branch_user(user=user, target_user_id=target_user_id)


# ─── Local staff records (no login) ────────────────────────

@router.post("")
async def create_staff(
    body: CreateStaffIn,
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.create_branch_user(
        user=user,
        branch_id=body.branch_id,
        email=body.email,
        name=body.name,
        role=body.role,
        phone=body.phone,
    )


@router.get("")
async def list_staff(
    branch_id: Optional[str] = None,
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.get_branch_users(user=user, branch_id=branch_id)


@router.patch("/{staff_id}")
async def update_staff(
    staff_id: str,
    body: UpdateStaffIn,
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.update_branch_user(
        user=user,
        branch_user_id=staff_id,
        **body.model_dump(exclude_none=True),
    )


@router.delete("/{staff_id}")
async def deactivate_staff(
    staff_id: str,
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.deactivate_branch_user(user=user, branch_user_id=staff_id)
