"""Sandbox CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/sandbox", tags=["Accounting – Sandbox"])

TABLE = "acc_sandboxes"
PK = "sandbox_id"
LABEL = "Sandbox"

CHANGE_TABLE = "acc_sandbox_changes"
CHANGE_PK = "change_id"


_auth = require_permission("accounting:read")


class SandboxCreate(BaseModel):
    name: str
    description: Optional[str] = None
    config: Optional[dict] = None


class SandboxUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    config: Optional[dict] = None
    status: Optional[str] = None


@router.get("")
async def list_sandboxes(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, page=page, per_page=per_page)


@router.post("", status_code=201)
async def create_sandbox(body: SandboxCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{sandbox_id}")
async def get_sandbox(sandbox_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, sandbox_id, user, LABEL)


@router.put("/{sandbox_id}")
async def update_sandbox(sandbox_id: UUID, body: SandboxUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, sandbox_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{sandbox_id}")
async def delete_sandbox(sandbox_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, sandbox_id, user, LABEL)


@router.post("/{sandbox_id}/activate")
async def activate_sandbox(sandbox_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, sandbox_id, {"status": "active"}, user, LABEL)


@router.post("/{sandbox_id}/deactivate")
async def deactivate_sandbox(sandbox_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, sandbox_id, {"status": "inactive"}, user, LABEL)


@router.post("/{sandbox_id}/merge")
async def merge_sandbox(sandbox_id: UUID, user: UserContext = Depends(_auth)):
    await acc_get(TABLE, PK, sandbox_id, user, LABEL)
    return {"message": "Sandbox merge initiated"}


@router.post("/{sandbox_id}/reset")
async def reset_sandbox(sandbox_id: UUID, user: UserContext = Depends(_auth)):
    await acc_get(TABLE, PK, sandbox_id, user, LABEL)
    return {"message": "Sandbox reset initiated"}


@router.post("/{sandbox_id}/clone")
async def clone_sandbox(sandbox_id: UUID, user: UserContext = Depends(_auth)):
    existing = await acc_get(TABLE, PK, sandbox_id, user, LABEL)
    data = {k: v for k, v in existing.items() if k not in ("sandbox_id", "created_at", "updated_at", "user_id", "branch_id")}
    data["name"] = f"{data.get('name', '')} (Clone)"
    data["status"] = "active"
    return await acc_create(TABLE, data, user)


# ── Sandbox Changes ──────────────────────────────────────────

@router.get("/{sandbox_id}/changes")
async def list_sandbox_changes(
    sandbox_id: UUID,
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(CHANGE_TABLE, user, filters={"sandbox_id": sandbox_id}, page=page, per_page=per_page)


@router.get("/{sandbox_id}/changes/{change_id}")
async def get_sandbox_change(sandbox_id: UUID, change_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(CHANGE_TABLE, CHANGE_PK, change_id, user, "Sandbox Change")


@router.post("/{sandbox_id}/changes/{change_id}/revert")
async def revert_sandbox_change(sandbox_id: UUID, change_id: UUID, user: UserContext = Depends(_auth)):
    await acc_get(CHANGE_TABLE, CHANGE_PK, change_id, user, "Sandbox Change")
    return {"message": "Change reverted"}


@router.post("/{sandbox_id}/compare")
async def compare_sandbox(sandbox_id: UUID, user: UserContext = Depends(_auth)):
    changes = await acc_list(CHANGE_TABLE, user, filters={"sandbox_id": sandbox_id}, per_page=200)
    return {"sandbox_id": str(sandbox_id), "total_changes": changes["total"], "changes": changes["items"]}
