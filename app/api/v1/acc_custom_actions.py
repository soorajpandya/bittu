"""Custom Actions CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/customactions", tags=["Accounting – Custom Actions"])

TABLE = "acc_custom_actions"
PK = "action_id"
LABEL = "Custom Action"


_auth = require_permission("accounting:read")


class ActionCreate(BaseModel):
    name: str
    module: Optional[str] = None
    action_type: Optional[str] = None
    config: Optional[dict] = None
    is_active: bool = True


class ActionUpdate(BaseModel):
    name: Optional[str] = None
    module: Optional[str] = None
    action_type: Optional[str] = None
    config: Optional[dict] = None
    is_active: Optional[bool] = None


@router.get("")
async def list_custom_actions(
    user: UserContext = Depends(_auth),
    module: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"module": module}, page=page, per_page=per_page)


@router.post("", status_code=201)
async def create_custom_action(body: ActionCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{action_id}")
async def get_custom_action(action_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, action_id, user, LABEL)


@router.put("/{action_id}")
async def update_custom_action(action_id: UUID, body: ActionUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, action_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{action_id}")
async def delete_custom_action(action_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, action_id, user, LABEL)
