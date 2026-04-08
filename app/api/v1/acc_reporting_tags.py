"""Reporting Tags CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.core.database import get_connection
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete, acc_status_update

router = APIRouter(prefix="/accounting/reportingtags", tags=["Accounting – Reporting Tags"])

TABLE = "acc_reporting_tags"
PK = "tag_id"
LABEL = "Reporting Tag"


_auth = require_permission("accounting:read")


class TagOption(BaseModel):
    option_name: str
    is_active: bool = True


class ReportingTagCreate(BaseModel):
    tag_name: str
    tag_options: Optional[list[TagOption]] = None
    custom_fields: Optional[list] = None


class ReportingTagUpdate(BaseModel):
    tag_name: Optional[str] = None
    tag_options: Optional[list[TagOption]] = None
    custom_fields: Optional[list] = None


class ReorderItem(BaseModel):
    tag_id: UUID
    sort_order: int


class OptionsUpdate(BaseModel):
    options: list


class CriteriaUpdate(BaseModel):
    criteria: dict


class DefaultOption(BaseModel):
    option_id: str


@router.get("")
async def list_reporting_tags(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, page=page, per_page=per_page, search_fields=["tag_name"])


@router.post("", status_code=201)
async def create_reporting_tag(body: ReportingTagCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    if data.get("tag_options"):
        data["tag_options"] = [o.model_dump() if hasattr(o, "model_dump") else o for o in data["tag_options"]]
    return await acc_create(TABLE, data, user)


@router.get("/options")
async def get_all_tag_options(user: UserContext = Depends(_auth)):
    if user.is_branch_user:
        params = [user.owner_id, user.branch_id]
        clause = "user_id = $1 AND branch_id = $2"
    else:
        params = [user.user_id]
        clause = "user_id = $1"
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"SELECT tag_id, tag_name, tag_options FROM {TABLE} WHERE {clause}", *params
        )
    result = []
    for r in rows:
        for opt in (r["tag_options"] or []):
            opt["tag_id"] = str(r["tag_id"])
            opt["tag_name"] = r["tag_name"]
            result.append(opt)
    return {"items": result}


@router.put("/reorder")
async def reorder_tags(body: list[ReorderItem], user: UserContext = Depends(_auth)):
    async with get_connection() as conn:
        for item in body:
            if user.is_branch_user:
                params = [item.sort_order, user.owner_id, user.branch_id, item.tag_id]
                clause = "user_id = $2 AND branch_id = $3"
            else:
                params = [item.sort_order, user.user_id, item.tag_id]
                clause = "user_id = $2"
            await conn.execute(
                f"UPDATE {TABLE} SET sort_order = $1, updated_at = now() WHERE {clause} AND {PK} = ${len(params)}",
                *params,
            )
    return {"message": "Reporting tags reordered"}


@router.get("/{tag_id}")
async def get_reporting_tag(tag_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, tag_id, user, LABEL)


@router.put("/{tag_id}")
async def update_reporting_tag(tag_id: UUID, body: ReportingTagUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    if data.get("tag_options"):
        data["tag_options"] = [o.model_dump() if hasattr(o, "model_dump") else o for o in data["tag_options"]]
    return await acc_update(TABLE, PK, tag_id, data, user, LABEL)


@router.delete("/{tag_id}")
async def delete_reporting_tag(tag_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, tag_id, user, LABEL)


# ── Extra endpoints ──────────────────────────────────────────────

@router.post("/{tag_id}/default")
async def mark_default_option(tag_id: UUID, body: DefaultOption, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, tag_id, {"default_option_id": body.option_id}, user, LABEL)


@router.put("/{tag_id}/options")
async def update_tag_options(tag_id: UUID, body: OptionsUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, tag_id, {"tag_options": body.options}, user, LABEL)


@router.put("/{tag_id}/criteria")
async def update_tag_criteria(tag_id: UUID, body: CriteriaUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, tag_id, {"criteria": body.criteria}, user, LABEL)


@router.post("/{tag_id}/active")
async def activate_tag(tag_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, tag_id, "active", user, LABEL)


@router.post("/{tag_id}/inactive")
async def deactivate_tag(tag_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, tag_id, "inactive", user, LABEL)


@router.post("/{tag_id}/option/{option_id}/active")
async def activate_tag_option(tag_id: UUID, option_id: str, user: UserContext = Depends(_auth)):
    record = await acc_get(TABLE, PK, tag_id, user, LABEL)
    options = record.get("tag_options") or []
    updated = False
    for opt in options:
        if opt.get("option_id") == option_id:
            opt["is_active"] = True
            updated = True
            break
    if not updated:
        raise HTTPException(status_code=404, detail="Option not found")
    return await acc_update(TABLE, PK, tag_id, {"tag_options": options}, user, LABEL)


@router.post("/{tag_id}/option/{option_id}/inactive")
async def deactivate_tag_option(tag_id: UUID, option_id: str, user: UserContext = Depends(_auth)):
    record = await acc_get(TABLE, PK, tag_id, user, LABEL)
    options = record.get("tag_options") or []
    updated = False
    for opt in options:
        if opt.get("option_id") == option_id:
            opt["is_active"] = False
            updated = True
            break
    if not updated:
        raise HTTPException(status_code=404, detail="Option not found")
    return await acc_update(TABLE, PK, tag_id, {"tag_options": options}, user, LABEL)


@router.get("/{tag_id}/options/all")
async def get_tag_options(tag_id: UUID, user: UserContext = Depends(_auth)):
    record = await acc_get(TABLE, PK, tag_id, user, LABEL)
    return {"items": record.get("tag_options") or []}
