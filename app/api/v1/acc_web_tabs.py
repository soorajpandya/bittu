"""Web Tabs CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/web-tabs", tags=["Accounting – Web Tabs"])

TABLE = "acc_web_tabs"
PK = "web_tab_id"
LABEL = "Web Tab"


_auth = require_permission("accounting:read")


class WebTabCreate(BaseModel):
    name: str
    url: str
    module: Optional[str] = None
    description: Optional[str] = None
    config: Optional[dict] = None


class WebTabUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    module: Optional[str] = None
    description: Optional[str] = None
    config: Optional[dict] = None
    status: Optional[str] = None


@router.get("")
async def list_web_tabs(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, page=page, per_page=per_page)


@router.post("", status_code=201)
async def create_web_tab(body: WebTabCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{web_tab_id}")
async def get_web_tab(web_tab_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, web_tab_id, user, LABEL)


@router.put("/{web_tab_id}")
async def update_web_tab(web_tab_id: UUID, body: WebTabUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, web_tab_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{web_tab_id}")
async def delete_web_tab(web_tab_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, web_tab_id, user, LABEL)


@router.post("/{web_tab_id}/activate")
async def activate_web_tab(web_tab_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, web_tab_id, {"status": "active"}, user, LABEL)


@router.post("/{web_tab_id}/deactivate")
async def deactivate_web_tab(web_tab_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, web_tab_id, {"status": "inactive"}, user, LABEL)
