"""Tasks CRUD endpoints (project tasks)."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import (
    acc_list, acc_get, acc_create, acc_update, acc_delete,
    acc_comments_list, acc_comment_add, acc_comment_delete,
    acc_attachment_get, acc_attachment_add, acc_attachment_delete,
    acc_bulk_update, acc_bulk_delete, acc_status_update,
)

router = APIRouter(prefix="/accounting/projects/{project_id}/tasks", tags=["Accounting – Tasks"])

TABLE = "acc_tasks"
PK = "task_id"
LABEL = "Task"


_auth = require_permission("accounting:read")


class TaskCreate(BaseModel):
    task_name: str
    description: Optional[str] = None
    rate: float = 0
    budget_hours: float = 0
    is_billable: bool = True
    status: str = "active"
    custom_fields: Optional[list] = None


class TaskUpdate(BaseModel):
    task_name: Optional[str] = None
    description: Optional[str] = None
    rate: Optional[float] = None
    budget_hours: Optional[float] = None
    is_billable: Optional[bool] = None
    status: Optional[str] = None
    custom_fields: Optional[list] = None


class BulkTaskUpdate(BaseModel):
    task_ids: list[UUID]
    data: dict


class BulkTaskDelete(BaseModel):
    task_ids: list[UUID]


class PercentageInput(BaseModel):
    percentage: float


class CommentInput(BaseModel):
    description: str


class AttachmentInput(BaseModel):
    file_name: str
    file_type: str
    file_size_formatted: str


@router.get("")
async def list_tasks(
    project_id: UUID,
    user: UserContext = Depends(_auth),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"project_id": project_id, "status": status}, page=page, per_page=per_page, search_fields=["task_name"])


@router.post("", status_code=201)
async def create_task(project_id: UUID, body: TaskCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    data["project_id"] = str(project_id)
    return await acc_create(TABLE, data, user)


@router.get("/{task_id}")
async def get_task(project_id: UUID, task_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, task_id, user, LABEL)


@router.put("/{task_id}")
async def update_task(project_id: UUID, task_id: UUID, body: TaskUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, task_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{task_id}")
async def delete_task(project_id: UUID, task_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, task_id, user, LABEL)


# ── Bulk operations ──────────────────────────────────────────

@router.put("")
async def bulk_update_tasks(project_id: UUID, body: BulkTaskUpdate, user: UserContext = Depends(_auth)):
    return await acc_bulk_update(TABLE, PK, body.task_ids, body.data, user, LABEL)


@router.post("/bulk-delete")
async def bulk_delete_tasks(project_id: UUID, body: BulkTaskDelete, user: UserContext = Depends(_auth)):
    return await acc_bulk_delete(TABLE, PK, body.task_ids, user, LABEL)


# ── Percentage / status ──────────────────────────────────────

@router.post("/{task_id}/percentage")
async def update_task_percentage(project_id: UUID, task_id: UUID, body: PercentageInput, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, task_id, {"percentage": body.percentage}, user, LABEL)


@router.post("/{task_id}/markasopen")
async def mark_task_open(project_id: UUID, task_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, task_id, "open", user, LABEL)


@router.post("/{task_id}/markasongoing")
async def mark_task_ongoing(project_id: UUID, task_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, task_id, "ongoing", user, LABEL)


@router.post("/{task_id}/markascompleted")
async def mark_task_completed(project_id: UUID, task_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, task_id, "completed", user, LABEL)


# ── Comments ─────────────────────────────────────────────────

@router.get("/{task_id}/comments")
async def list_task_comments(project_id: UUID, task_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comments_list(TABLE, PK, task_id, user, LABEL)


@router.post("/{task_id}/comments", status_code=201)
async def add_task_comment(project_id: UUID, task_id: UUID, body: CommentInput, user: UserContext = Depends(_auth)):
    return await acc_comment_add(TABLE, PK, task_id, body.model_dump(), user, LABEL)


@router.delete("/{task_id}/comments/{comment_id}")
async def delete_task_comment(project_id: UUID, task_id: UUID, comment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comment_delete(TABLE, PK, task_id, comment_id, user, LABEL)


# ── Attachments / Documents ──────────────────────────────────

@router.post("/{task_id}/attachment", status_code=201)
async def add_task_attachment(project_id: UUID, task_id: UUID, body: AttachmentInput, user: UserContext = Depends(_auth)):
    return await acc_attachment_add(TABLE, PK, task_id, body.model_dump(), user, LABEL)


@router.get("/{task_id}/documents/{document_id}")
async def get_task_document(project_id: UUID, task_id: UUID, document_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_get(TABLE, PK, task_id, document_id, user, LABEL)


@router.delete("/{task_id}/documents/{document_id}")
async def delete_task_document(project_id: UUID, task_id: UUID, document_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_attachment_delete(TABLE, PK, task_id, document_id, user, LABEL)
