"""Integrations CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/integrations", tags=["Accounting – Integrations"])

TABLE = "acc_integrations"
PK = "integration_id"
LABEL = "Integration"


_auth = require_permission("accounting:read")


class IntegrationCreate(BaseModel):
    name: str
    service: Optional[str] = None
    config: Optional[dict] = None
    is_active: bool = True


class IntegrationUpdate(BaseModel):
    name: Optional[str] = None
    service: Optional[str] = None
    config: Optional[dict] = None
    is_active: Optional[bool] = None


@router.get("")
async def list_integrations(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, page=page, per_page=per_page)


@router.post("", status_code=201)
async def create_integration(body: IntegrationCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{integration_id}")
async def get_integration(integration_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, integration_id, user, LABEL)


@router.put("/{integration_id}")
async def update_integration(integration_id: UUID, body: IntegrationUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, integration_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)
