"""Related Lists CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/relatedlists", tags=["Accounting – Related Lists"])

TABLE = "acc_related_lists"
PK = "related_list_id"
LABEL = "Related List"


_auth = require_permission("accounting:read")


class RelatedListCreate(BaseModel):
    name: str
    module: Optional[str] = None
    related_module: Optional[str] = None
    field_mapping: Optional[dict] = None
    is_active: bool = True


class RelatedListUpdate(BaseModel):
    name: Optional[str] = None
    module: Optional[str] = None
    related_module: Optional[str] = None
    field_mapping: Optional[dict] = None
    is_active: Optional[bool] = None


@router.get("")
async def list_related_lists(
    user: UserContext = Depends(_auth),
    module: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"module": module}, page=page, per_page=per_page)


@router.post("", status_code=201)
async def create_related_list(body: RelatedListCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{related_list_id}")
async def get_related_list(related_list_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, related_list_id, user, LABEL)


@router.put("/{related_list_id}")
async def update_related_list(related_list_id: UUID, body: RelatedListUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, related_list_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{related_list_id}")
async def delete_related_list(related_list_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, related_list_id, user, LABEL)


@router.post("/{related_list_id}/active")
async def activate_related_list(related_list_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, related_list_id, {"is_active": True}, user, LABEL)


@router.post("/{related_list_id}/inactive")
async def deactivate_related_list(related_list_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, related_list_id, {"is_active": False}, user, LABEL)
