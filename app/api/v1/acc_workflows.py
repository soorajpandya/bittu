"""Workflows CRUD endpoints."""
import uuid as _uuid
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import (
    acc_list, acc_get, acc_create, acc_update, acc_delete,
    acc_sub_list, acc_sub_create, acc_sub_get, acc_sub_update, acc_sub_delete,
)

router = APIRouter(prefix="/accounting/workflows", tags=["Accounting – Workflows"])

TABLE = "acc_workflows"
PK = "workflow_id"
LABEL = "Workflow"


_auth = require_permission("accounting:read")


class WorkflowCreate(BaseModel):
    workflow_name: str
    module: str
    trigger_type: str = "entity_action"
    trigger_entity_action: Optional[str] = None
    criteria: Optional[list[dict]] = None
    actions: Optional[list[dict]] = None
    is_active: bool = True
    custom_fields: Optional[list] = None


class WorkflowUpdate(BaseModel):
    workflow_name: Optional[str] = None
    module: Optional[str] = None
    trigger_type: Optional[str] = None
    trigger_entity_action: Optional[str] = None
    criteria: Optional[list[dict]] = None
    actions: Optional[list[dict]] = None
    is_active: Optional[bool] = None
    custom_fields: Optional[list] = None


class ReorderItem(BaseModel):
    workflow_id: UUID
    sort_order: int


class CustomTriggerCreate(BaseModel):
    name: str
    description: Optional[str] = None
    module: str
    is_active: bool = True


class CustomTriggerUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    module: Optional[str] = None
    is_active: Optional[bool] = None


class RetryPolicyCreate(BaseModel):
    name: str
    retry_count: int
    retry_interval: int
    description: Optional[str] = None


class RetryPolicyUpdate(BaseModel):
    name: Optional[str] = None
    retry_count: Optional[int] = None
    retry_interval: Optional[int] = None
    description: Optional[str] = None


CT_TABLE = "acc_custom_triggers"
CT_PK = "trigger_id"
CT_LABEL = "Custom Trigger"

RP_TABLE = "acc_retry_policies"
RP_PK = "policy_id"
RP_LABEL = "Retry Policy"

LOG_TABLE = "acc_workflow_logs"
LOG_PK = "log_id"
LOG_LABEL = "Workflow Log"


@router.get("")
async def list_workflows(
    user: UserContext = Depends(_auth),
    module: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"module": module}, page=page, per_page=per_page, search_fields=["workflow_name"])


@router.post("", status_code=201)
async def create_workflow(body: WorkflowCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{workflow_id}")
async def get_workflow(workflow_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, workflow_id, user, LABEL)


@router.put("/{workflow_id}")
async def update_workflow(workflow_id: UUID, body: WorkflowUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, workflow_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{workflow_id}")
async def delete_workflow(workflow_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, workflow_id, user, LABEL)


# ── 1. Activate workflow ───────────────────────────────────────────
@router.post("/{workflow_id}/active")
async def activate_workflow(workflow_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, workflow_id, {"is_active": True}, user, LABEL)


# ── 2. Deactivate workflow ─────────────────────────────────────────
@router.post("/{workflow_id}/inactive")
async def deactivate_workflow(workflow_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, workflow_id, {"is_active": False}, user, LABEL)


# ── 3. Reorder workflows ──────────────────────────────────────────
@router.post("/reorder")
async def reorder_workflows(items: list[ReorderItem], user: UserContext = Depends(_auth)):
    results = []
    for item in items:
        r = await acc_update(TABLE, PK, item.workflow_id, {"sort_order": item.sort_order}, user, LABEL)
        results.append(r)
    return {"message": "Workflows reordered", "updated": len(results)}


# ── 4. Edit page data ─────────────────────────────────────────────
@router.get("/editpage")
async def get_workflow_edit_page(
    workflow_id: UUID = Query(...),
    user: UserContext = Depends(_auth),
):
    return await acc_get(TABLE, PK, workflow_id, user, LABEL)


# ── 5. Module filters ─────────────────────────────────────────────
@router.get("/settings/automation/modulefilters")
async def get_module_filters(user: UserContext = Depends(_auth)):
    return {
        "modules": [
            "invoices", "bills", "expenses", "purchase_orders",
            "sales_orders", "estimates", "credit_notes", "vendor_credits",
            "journals", "contacts", "items", "projects",
        ]
    }


# ── 6. Trigger workflow ───────────────────────────────────────────
@router.post("/{workflow_id}/trigger")
async def trigger_workflow(workflow_id: UUID, user: UserContext = Depends(_auth)):
    return {"message": f"Workflow {workflow_id} triggered", "status": "queued"}


# ── 7. Trigger workflow action ─────────────────────────────────────
@router.post("/{workflow_id}/triggeraction")
async def trigger_workflow_action(workflow_id: UUID, user: UserContext = Depends(_auth)):
    return {"message": f"Workflow action for {workflow_id} triggered", "status": "queued"}


# ── 8. List custom triggers ───────────────────────────────────────
@router.get("/settings/customtriggers")
async def list_custom_triggers(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(CT_TABLE, user, page=page, per_page=per_page, search_fields=["name"])


# ── 9. Create custom trigger ─────────────────────────────────────
@router.post("/settings/customtriggers", status_code=201)
async def create_custom_trigger(body: CustomTriggerCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    data["api_key"] = str(_uuid.uuid4())
    return await acc_create(CT_TABLE, data, user)


# ── 10. Custom trigger edit page ──────────────────────────────────
@router.get("/settings/customtriggers/editpage")
async def get_custom_trigger_edit_page(
    trigger_id: UUID = Query(...),
    user: UserContext = Depends(_auth),
):
    return await acc_get(CT_TABLE, CT_PK, trigger_id, user, CT_LABEL)


# ── 11. Update custom trigger ────────────────────────────────────
@router.put("/settings/customtriggers/{trigger_id}")
async def update_custom_trigger(trigger_id: UUID, body: CustomTriggerUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(CT_TABLE, CT_PK, trigger_id, body.model_dump(exclude_unset=True, exclude_none=True), user, CT_LABEL)


# ── 12. Delete custom trigger ────────────────────────────────────
@router.delete("/settings/customtriggers/{trigger_id}")
async def delete_custom_trigger(trigger_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(CT_TABLE, CT_PK, trigger_id, user, CT_LABEL)


# ── 13. Regenerate API key ───────────────────────────────────────
@router.put("/settings/customtriggers/{trigger_id}/regenerate")
async def regenerate_custom_trigger_api_key(trigger_id: UUID, user: UserContext = Depends(_auth)):
    new_key = str(_uuid.uuid4())
    return await acc_update(CT_TABLE, CT_PK, trigger_id, {"api_key": new_key}, user, CT_LABEL)


# ── 14. Execute custom trigger ───────────────────────────────────
@router.post("/settings/customtriggers/{trigger_id}/execute")
async def execute_custom_trigger(trigger_id: UUID, user: UserContext = Depends(_auth)):
    return {"message": f"Custom trigger {trigger_id} executed", "status": "queued"}


# ── 15. Get trigger URL ──────────────────────────────────────────
@router.get("/settings/customtriggers/{trigger_id}/url/fetch")
async def get_custom_trigger_url(trigger_id: UUID, user: UserContext = Depends(_auth)):
    row = await acc_get(CT_TABLE, CT_PK, trigger_id, user, CT_LABEL)
    api_key = row.get("api_key", "") if isinstance(row, dict) else ""
    return {"trigger_id": str(trigger_id), "url": f"/api/v1/webhooks/trigger/{trigger_id}?api_key={api_key}"}


# ── 16. List retry policies ──────────────────────────────────────
@router.get("/retrypolicies")
async def list_retry_policies(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(RP_TABLE, user, page=page, per_page=per_page, search_fields=["name"])


# ── 17. Create retry policy ──────────────────────────────────────
@router.post("/retrypolicies", status_code=201)
async def create_retry_policy(body: RetryPolicyCreate, user: UserContext = Depends(_auth)):
    return await acc_create(RP_TABLE, body.model_dump(exclude_none=True), user)


# ── 18. Get retry policy ────────────────────────────────────────
@router.get("/retrypolicies/{policy_id}")
async def get_retry_policy(policy_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(RP_TABLE, RP_PK, policy_id, user, RP_LABEL)


# ── 19. Update retry policy ─────────────────────────────────────
@router.put("/retrypolicies/{policy_id}")
async def update_retry_policy(policy_id: UUID, body: RetryPolicyUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(RP_TABLE, RP_PK, policy_id, body.model_dump(exclude_unset=True, exclude_none=True), user, RP_LABEL)


# ── 20. Delete retry policy ─────────────────────────────────────
@router.delete("/retrypolicies/{policy_id}")
async def delete_retry_policy(policy_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(RP_TABLE, RP_PK, policy_id, user, RP_LABEL)


# ── 21. List workflow logs ───────────────────────────────────────
@router.get("/reports/workflowlogs")
async def list_workflow_logs(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(LOG_TABLE, user, page=page, per_page=per_page, search_fields=["workflow_name"])


# ── 22. Workflow log details ─────────────────────────────────────
@router.get("/reports/workflowlogs/details")
async def get_workflow_log_details(
    log_id: UUID = Query(...),
    user: UserContext = Depends(_auth),
):
    return await acc_get(LOG_TABLE, LOG_PK, log_id, user, LOG_LABEL)


# ── 23. Upcoming workflows ──────────────────────────────────────
@router.get("/reports/upcomingworkflows")
async def list_upcoming_workflows(user: UserContext = Depends(_auth)):
    return {"upcoming_workflows": []}


# ── 24. Upcoming actions ─────────────────────────────────────────
@router.get("/reports/upcomingactions")
async def list_upcoming_actions(user: UserContext = Depends(_auth)):
    return {"upcoming_actions": []}


# ── 25. Failed workflows ─────────────────────────────────────────
@router.get("/reports/failedworkflows")
async def list_failed_workflows(user: UserContext = Depends(_auth)):
    return {"failed_workflows": []}


# ── 26. Failed workflow actions ──────────────────────────────────
@router.get("/reports/failedworkflowactions")
async def list_failed_workflow_actions(user: UserContext = Depends(_auth)):
    return {"failed_workflow_actions": []}
