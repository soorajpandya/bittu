"""Module Renaming CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/modulerenaming", tags=["Accounting – Module Renaming"])

TABLE = "acc_module_renames"
PK = "rename_id"
LABEL = "Module Rename"


_auth = require_permission("accounting:read")


class RenameCreate(BaseModel):
    original_name: str
    custom_name: str
    module_type: Optional[str] = None


class RenameUpdate(BaseModel):
    custom_name: Optional[str] = None
    module_type: Optional[str] = None


@router.get("")
async def list_module_renames(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, page=page, per_page=per_page)


@router.put("")
async def update_module_renames(body: list[RenameCreate], user: UserContext = Depends(_auth)):
    results = []
    for item in body:
        results.append(await acc_create(TABLE, item.model_dump(exclude_none=True), user))
    return results
