"""Blueprints CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/blueprints", tags=["Accounting – Blueprints"])

TABLE = "acc_blueprints"
PK = "blueprint_id"
LABEL = "Blueprint"


_auth = require_permission("accounting:read")


class Transition(BaseModel):
    from_status: str
    to_status: str
    conditions: Optional[list[dict]] = None
    actions: Optional[list[dict]] = None


class BlueprintCreate(BaseModel):
    blueprint_name: str
    module: str
    transitions: Optional[list[Transition]] = None
    custom_fields: Optional[list] = None


class BlueprintUpdate(BaseModel):
    blueprint_name: Optional[str] = None
    module: Optional[str] = None
    transitions: Optional[list[Transition]] = None
    custom_fields: Optional[list] = None


@router.get("")
async def list_blueprints(
    user: UserContext = Depends(_auth),
    module: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"module": module}, page=page, per_page=per_page, search_fields=["blueprint_name"])


@router.post("", status_code=201)
async def create_blueprint(body: BlueprintCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    if data.get("transitions"):
        data["transitions"] = [t.model_dump() if hasattr(t, "model_dump") else t for t in data["transitions"]]
    return await acc_create(TABLE, data, user)


@router.get("/{blueprint_id}")
async def get_blueprint(blueprint_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, blueprint_id, user, LABEL)


@router.put("/{blueprint_id}")
async def update_blueprint(blueprint_id: UUID, body: BlueprintUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    if data.get("transitions"):
        data["transitions"] = [t.model_dump() if hasattr(t, "model_dump") else t for t in data["transitions"]]
    return await acc_update(TABLE, PK, blueprint_id, data, user, LABEL)


@router.delete("/{blueprint_id}")
async def delete_blueprint(blueprint_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, blueprint_id, user, LABEL)
