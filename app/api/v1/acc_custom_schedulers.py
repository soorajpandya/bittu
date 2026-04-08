"""Custom Schedulers CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/customschedulers", tags=["Accounting – Custom Schedulers"])

TABLE = "acc_custom_schedulers"
PK = "scheduler_id"
LABEL = "Custom Scheduler"


_auth = require_permission("accounting:read")


class SchedulerCreate(BaseModel):
    name: str
    function_id: Optional[UUID] = None
    frequency: Optional[str] = None
    cron_expression: Optional[str] = None
    is_active: bool = True
    description: Optional[str] = None


class SchedulerUpdate(BaseModel):
    name: Optional[str] = None
    function_id: Optional[UUID] = None
    frequency: Optional[str] = None
    cron_expression: Optional[str] = None
    is_active: Optional[bool] = None
    description: Optional[str] = None


@router.get("")
async def list_custom_schedulers(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, page=page, per_page=per_page)


@router.post("", status_code=201)
async def create_custom_scheduler(body: SchedulerCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{scheduler_id}")
async def get_custom_scheduler(scheduler_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, scheduler_id, user, LABEL)


@router.put("/{scheduler_id}")
async def update_custom_scheduler(scheduler_id: UUID, body: SchedulerUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, scheduler_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{scheduler_id}")
async def delete_custom_scheduler(scheduler_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, scheduler_id, user, LABEL)
