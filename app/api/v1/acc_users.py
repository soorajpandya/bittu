"""Accounting Users CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete, acc_status_update

router = APIRouter(prefix="/accounting/users", tags=["Accounting – Users"])

TABLE = "acc_users"
PK = "acc_user_id"
LABEL = "User"


_auth = require_permission("accounting:read")


class AccUserCreate(BaseModel):
    name: str
    email: str
    role_id: Optional[str] = None
    is_active: bool = True
    cost_rate: float = 0
    custom_fields: Optional[list] = None


class AccUserUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    role_id: Optional[str] = None
    is_active: Optional[bool] = None
    cost_rate: Optional[float] = None
    custom_fields: Optional[list] = None


@router.get("")
async def list_acc_users(
    user: UserContext = Depends(_auth),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"status": status}, page=page, per_page=per_page, search_fields=["name", "email"])


@router.post("", status_code=201)
async def create_acc_user(body: AccUserCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{acc_user_id}")
async def get_acc_user(acc_user_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, acc_user_id, user, LABEL)


@router.put("/{acc_user_id}")
async def update_acc_user(acc_user_id: UUID, body: AccUserUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, acc_user_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{acc_user_id}")
async def delete_acc_user(acc_user_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, acc_user_id, user, LABEL)


@router.post("/{acc_user_id}/active")
async def mark_active(acc_user_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, acc_user_id, "active", user, LABEL)


@router.post("/{acc_user_id}/inactive")
async def mark_inactive(acc_user_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, acc_user_id, "inactive", user, LABEL)
