"""Custom Views CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/customviews", tags=["Accounting – Custom Views"])

TABLE = "acc_custom_views"
PK = "custom_view_id"
LABEL = "Custom View"


_auth = require_permission("accounting:read")


class CustomViewCreate(BaseModel):
    view_name: str
    module: str  # e.g. "invoices", "contacts"
    criteria: Optional[list[dict]] = None
    sort_column: Optional[str] = None
    sort_order: str = "ascending"
    custom_fields: Optional[list] = None


class CustomViewUpdate(BaseModel):
    view_name: Optional[str] = None
    module: Optional[str] = None
    criteria: Optional[list[dict]] = None
    sort_column: Optional[str] = None
    sort_order: Optional[str] = None
    custom_fields: Optional[list] = None


@router.get("")
async def list_custom_views(
    user: UserContext = Depends(_auth),
    module: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"module": module}, page=page, per_page=per_page, search_fields=["view_name"])


@router.post("", status_code=201)
async def create_custom_view(body: CustomViewCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{custom_view_id}")
async def get_custom_view(custom_view_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, custom_view_id, user, LABEL)


@router.put("/{custom_view_id}")
async def update_custom_view(custom_view_id: UUID, body: CustomViewUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, custom_view_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{custom_view_id}")
async def delete_custom_view(custom_view_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, custom_view_id, user, LABEL)
