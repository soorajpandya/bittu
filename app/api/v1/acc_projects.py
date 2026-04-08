"""Projects CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import (
    acc_list, acc_get, acc_create, acc_update, acc_delete, acc_status_update,
    acc_comments_list, acc_comment_add, acc_comment_delete,
    acc_sub_list, acc_sub_create, acc_sub_get, acc_sub_update, acc_sub_delete,
)

router = APIRouter(prefix="/accounting/projects", tags=["Accounting – Projects"])

TABLE = "acc_projects"
PK = "project_id"
LABEL = "Project"


_auth = require_permission("accounting:read")


class ProjectCreate(BaseModel):
    project_name: str
    customer_id: Optional[UUID] = None
    description: Optional[str] = None
    billing_type: str = "fixed_cost_for_project"
    rate: float = 0
    budget_type: Optional[str] = None
    budget_hours: float = 0
    budget_amount: float = 0
    cost_budget: float = 0
    status: str = "active"
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


class ProjectUpdate(BaseModel):
    project_name: Optional[str] = None
    customer_id: Optional[UUID] = None
    description: Optional[str] = None
    billing_type: Optional[str] = None
    rate: Optional[float] = None
    budget_type: Optional[str] = None
    budget_hours: Optional[float] = None
    budget_amount: Optional[float] = None
    cost_budget: Optional[float] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


@router.get("")
async def list_projects(
    user: UserContext = Depends(_auth),
    customer_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"customer_id": customer_id, "status": status}, page=page, per_page=per_page, search_fields=["project_name"])


@router.post("", status_code=201)
async def create_project(body: ProjectCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{project_id}")
async def get_project(project_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, project_id, user, LABEL)


@router.put("/{project_id}")
async def update_project(project_id: UUID, body: ProjectUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, project_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{project_id}")
async def delete_project(project_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, project_id, user, LABEL)


@router.post("/{project_id}/status/active")
async def mark_active(project_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, project_id, "active", user, LABEL)


@router.post("/{project_id}/status/inactive")
async def mark_inactive(project_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, project_id, "inactive", user, LABEL)


# ── Pydantic models for sub-resources ────────────────────────────────────

class CommentInput(BaseModel):
    description: str
    show_comment_to_clients: Optional[bool] = False


class ProjectUserCreate(BaseModel):
    user_ref_id: Optional[UUID] = None
    role: Optional[str] = None
    rate: Optional[float] = None
    budget_hours: Optional[float] = None
    email: Optional[str] = None
    name: Optional[str] = None


class ProjectUserUpdate(BaseModel):
    user_ref_id: Optional[UUID] = None
    role: Optional[str] = None
    rate: Optional[float] = None
    budget_hours: Optional[float] = None
    email: Optional[str] = None
    name: Optional[str] = None


class TaskCreate(BaseModel):
    task_name: str
    description: Optional[str] = None
    rate: Optional[float] = None
    budget_hours: Optional[float] = None
    status: Optional[str] = "active"


class TaskUpdate(BaseModel):
    task_name: Optional[str] = None
    description: Optional[str] = None
    rate: Optional[float] = None
    budget_hours: Optional[float] = None
    status: Optional[str] = None


# ---------------------------------------------------------------------------
# 1. Update project via custom field
# ---------------------------------------------------------------------------

@router.put("")
async def update_project_by_custom_field(
    custom_field_name: str = Query(...),
    custom_field_value: str = Query(...),
    body: ProjectUpdate = ...,
    user: UserContext = Depends(_auth),
):
    return await acc_update(TABLE, PK, None, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL, custom_field_name=custom_field_name, custom_field_value=custom_field_value)


# ---------------------------------------------------------------------------
# 2. Clone project
# ---------------------------------------------------------------------------

@router.post("/{project_id}/clone", status_code=201)
async def clone_project(project_id: UUID, user: UserContext = Depends(_auth)):
    existing = await acc_get(TABLE, PK, project_id, user, LABEL)
    data = existing.copy()
    for key in (PK, "id", "created_at", "updated_at", "tenant_id"):
        data.pop(key, None)
    data["project_name"] = f"{data.get('project_name', '')} (Copy)"
    return await acc_create(TABLE, data, user)


# ---------------------------------------------------------------------------
# 3. List project users
# ---------------------------------------------------------------------------

@router.get("/{project_id}/users")
async def list_project_users(project_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_list("acc_project_users", "project_id", project_id, user)


# ---------------------------------------------------------------------------
# 4. Add project user
# ---------------------------------------------------------------------------

@router.post("/{project_id}/users", status_code=201)
async def add_project_user(project_id: UUID, body: ProjectUserCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    data["project_id"] = str(project_id)
    return await acc_sub_create("acc_project_users", data, user)


# ---------------------------------------------------------------------------
# 5. Invite project user
# ---------------------------------------------------------------------------

@router.post("/{project_id}/users/invite", status_code=201)
async def invite_project_user(project_id: UUID, body: ProjectUserCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    data["project_id"] = str(project_id)
    data["invited"] = True
    return await acc_sub_create("acc_project_users", data, user)


# ---------------------------------------------------------------------------
# 6. Get project user
# ---------------------------------------------------------------------------

@router.get("/{project_id}/users/{user_id}")
async def get_project_user(project_id: UUID, user_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_get("acc_project_users", "project_user_id", user_id, user, "Project User")


# ---------------------------------------------------------------------------
# 7. Update project user
# ---------------------------------------------------------------------------

@router.put("/{project_id}/users/{user_id}")
async def update_project_user(project_id: UUID, user_id: UUID, body: ProjectUserUpdate, user: UserContext = Depends(_auth)):
    return await acc_sub_update("acc_project_users", "project_user_id", user_id, body.model_dump(exclude_unset=True, exclude_none=True), user, "Project User")


# ---------------------------------------------------------------------------
# 8. Delete project user
# ---------------------------------------------------------------------------

@router.delete("/{project_id}/users/{user_id}")
async def delete_project_user(project_id: UUID, user_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_delete("acc_project_users", "project_user_id", user_id, user, "Project User")


# ---------------------------------------------------------------------------
# 9. List comments
# ---------------------------------------------------------------------------

@router.get("/{project_id}/comments")
async def list_comments(project_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comments_list(TABLE, PK, project_id, user, LABEL)


# ---------------------------------------------------------------------------
# 10. Add comment
# ---------------------------------------------------------------------------

@router.post("/{project_id}/comments", status_code=201)
async def add_comment(project_id: UUID, body: CommentInput, user: UserContext = Depends(_auth)):
    return await acc_comment_add(TABLE, PK, project_id, body.model_dump(exclude_none=True), user, LABEL)


# ---------------------------------------------------------------------------
# 11. Delete comment
# ---------------------------------------------------------------------------

@router.delete("/{project_id}/comments/{comment_id}")
async def delete_comment(project_id: UUID, comment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comment_delete(TABLE, PK, project_id, comment_id, user, LABEL)


# ---------------------------------------------------------------------------
# 12. List project invoices
# ---------------------------------------------------------------------------

@router.get("/{project_id}/invoices")
async def list_project_invoices(
    project_id: UUID,
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list("acc_invoices", user, filters={"project_id": project_id}, page=page, per_page=per_page)


# ---------------------------------------------------------------------------
# 13. List project tasks
# ---------------------------------------------------------------------------

@router.get("/{project_id}/tasks")
async def list_project_tasks(project_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_list("acc_tasks", "project_id", project_id, user)


# ---------------------------------------------------------------------------
# 14. Add task to project
# ---------------------------------------------------------------------------

@router.post("/{project_id}/tasks", status_code=201)
async def add_project_task(project_id: UUID, body: TaskCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    data["project_id"] = str(project_id)
    return await acc_sub_create("acc_tasks", data, user)


# ---------------------------------------------------------------------------
# 15. Get project task
# ---------------------------------------------------------------------------

@router.get("/{project_id}/tasks/{task_id}")
async def get_project_task(project_id: UUID, task_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_get("acc_tasks", "task_id", task_id, user, "Task")


# ---------------------------------------------------------------------------
# 16. Update project task
# ---------------------------------------------------------------------------

@router.put("/{project_id}/tasks/{task_id}")
async def update_project_task(project_id: UUID, task_id: UUID, body: TaskUpdate, user: UserContext = Depends(_auth)):
    return await acc_sub_update("acc_tasks", "task_id", task_id, body.model_dump(exclude_unset=True, exclude_none=True), user, "Task")


# ---------------------------------------------------------------------------
# 17. Delete project task
# ---------------------------------------------------------------------------

@router.delete("/{project_id}/tasks/{task_id}")
async def delete_project_task(project_id: UUID, task_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_delete("acc_tasks", "task_id", task_id, user, "Task")
