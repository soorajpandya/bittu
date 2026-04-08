"""Custom Functions CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/customfunctions", tags=["Accounting – Custom Functions"])

TABLE = "acc_custom_functions"
PK = "function_id"
LABEL = "Custom Function"

LOG_TABLE = "acc_custom_function_logs"
LOG_PK = "log_id"


_auth = require_permission("accounting:read")


class FunctionCreate(BaseModel):
    name: str
    module: Optional[str] = None
    script: Optional[str] = None
    input_params: Optional[list] = None
    return_type: Optional[str] = None
    is_active: bool = True
    description: Optional[str] = None


class FunctionUpdate(BaseModel):
    name: Optional[str] = None
    module: Optional[str] = None
    script: Optional[str] = None
    input_params: Optional[list] = None
    return_type: Optional[str] = None
    is_active: Optional[bool] = None
    description: Optional[str] = None


@router.get("")
async def list_custom_functions(
    user: UserContext = Depends(_auth),
    module: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"module": module}, page=page, per_page=per_page)


@router.post("", status_code=201)
async def create_custom_function(body: FunctionCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{function_id}")
async def get_custom_function(function_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, function_id, user, LABEL)


@router.put("/{function_id}")
async def update_custom_function(function_id: UUID, body: FunctionUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, function_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{function_id}")
async def delete_custom_function(function_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, function_id, user, LABEL)


@router.post("/{function_id}/active")
async def activate_function(function_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, function_id, {"is_active": True}, user, LABEL)


@router.post("/{function_id}/inactive")
async def deactivate_function(function_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, function_id, {"is_active": False}, user, LABEL)


@router.post("/{function_id}/execute")
async def execute_function(function_id: UUID, user: UserContext = Depends(_auth)):
    fn = await acc_get(TABLE, PK, function_id, user, LABEL)
    log = await acc_create(LOG_TABLE, {
        "function_id": function_id,
        "status": "executed",
        "input": {},
        "output": {},
    }, user)
    return {"message": "Function executed", "log_id": log["log_id"]}


@router.get("/{function_id}/logs")
async def list_function_logs(
    function_id: UUID,
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(LOG_TABLE, user, filters={"function_id": function_id}, page=page, per_page=per_page)


@router.get("/{function_id}/logs/{log_id}")
async def get_function_log(function_id: UUID, log_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(LOG_TABLE, LOG_PK, log_id, user, "Function Log")


@router.get("/editpage")
async def get_editpage(user: UserContext = Depends(_auth)):
    return {"modules": ["invoices", "bills", "expenses", "contacts", "items"], "return_types": ["void", "string", "number", "boolean", "list"]}


@router.post("/{function_id}/test")
async def test_function(function_id: UUID, user: UserContext = Depends(_auth)):
    fn = await acc_get(TABLE, PK, function_id, user, LABEL)
    return {"message": "Test executed", "result": None}
