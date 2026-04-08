"""Custom Modules CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/custommodules", tags=["Accounting – Custom Modules"])

TABLE = "acc_custom_modules"
PK = "module_id"
LABEL = "Custom Module"

RECORD_TABLE = "acc_custom_module_records"
RECORD_PK = "record_id"


_auth = require_permission("accounting:read")


class ModuleCreate(BaseModel):
    module_name: str
    api_name: Optional[str] = None
    description: Optional[str] = None
    fields: Optional[list] = None
    relationships: Optional[list] = None
    is_active: bool = True


class ModuleUpdate(BaseModel):
    module_name: Optional[str] = None
    api_name: Optional[str] = None
    description: Optional[str] = None
    fields: Optional[list] = None
    relationships: Optional[list] = None
    is_active: Optional[bool] = None


class RecordCreate(BaseModel):
    data: dict = {}


class RecordUpdate(BaseModel):
    data: Optional[dict] = None


@router.get("")
async def list_custom_modules(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, page=page, per_page=per_page)


@router.post("", status_code=201)
async def create_custom_module(body: ModuleCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{module_id}")
async def get_custom_module(module_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, module_id, user, LABEL)


@router.put("/{module_id}")
async def update_custom_module(module_id: UUID, body: ModuleUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, module_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{module_id}")
async def delete_custom_module(module_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, module_id, user, LABEL)


@router.post("/{module_id}/active")
async def activate_module(module_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, module_id, {"is_active": True}, user, LABEL)


@router.post("/{module_id}/inactive")
async def deactivate_module(module_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, module_id, {"is_active": False}, user, LABEL)


# ── Module Records ──────────────────────────────────────────────

@router.get("/{module_id}/records")
async def list_module_records(
    module_id: UUID,
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(RECORD_TABLE, user, filters={"module_id": module_id}, page=page, per_page=per_page)


@router.post("/{module_id}/records", status_code=201)
async def create_module_record(module_id: UUID, body: RecordCreate, user: UserContext = Depends(_auth)):
    d = body.model_dump()
    d["module_id"] = module_id
    return await acc_create(RECORD_TABLE, d, user)


@router.get("/{module_id}/records/{record_id}")
async def get_module_record(module_id: UUID, record_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(RECORD_TABLE, RECORD_PK, record_id, user, "Record")
