"""Time Entries CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/timeentries", tags=["Accounting – Time Entries"])

TABLE = "acc_time_entries"
PK = "time_entry_id"
LABEL = "Time Entry"


_auth = require_permission("accounting:read")


class TimeEntryCreate(BaseModel):
    project_id: UUID
    task_id: Optional[UUID] = None
    user_id_ref: Optional[UUID] = None
    log_date: Optional[str] = None
    log_time: Optional[str] = None  # HH:MM format
    begin_time: Optional[str] = None
    end_time: Optional[str] = None
    is_billable: bool = True
    notes: Optional[str] = None
    custom_fields: Optional[list] = None


class TimeEntryUpdate(BaseModel):
    project_id: Optional[UUID] = None
    task_id: Optional[UUID] = None
    user_id_ref: Optional[UUID] = None
    log_date: Optional[str] = None
    log_time: Optional[str] = None
    begin_time: Optional[str] = None
    end_time: Optional[str] = None
    is_billable: Optional[bool] = None
    notes: Optional[str] = None
    custom_fields: Optional[list] = None


@router.get("")
async def list_time_entries(
    user: UserContext = Depends(_auth),
    project_id: Optional[UUID] = Query(None),
    task_id: Optional[UUID] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"project_id": project_id, "task_id": task_id}, page=page, per_page=per_page, order_by="log_date DESC", search_fields=["notes"])


@router.post("", status_code=201)
async def create_time_entry(body: TimeEntryCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{time_entry_id}")
async def get_time_entry(time_entry_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, time_entry_id, user, LABEL)


@router.put("/{time_entry_id}")
async def update_time_entry(time_entry_id: UUID, body: TimeEntryUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, time_entry_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{time_entry_id}")
async def delete_time_entry(time_entry_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, time_entry_id, user, LABEL)
