"""Accounting Webhooks CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/webhooks", tags=["Accounting – Webhooks"])

TABLE = "acc_webhooks"
PK = "webhook_id"
LABEL = "Webhook"


_auth = require_permission("accounting:read")


class WebhookCreate(BaseModel):
    url: str
    events: list[str]  # e.g. ["invoice.created", "contact.updated"]
    is_active: bool = True
    secret: Optional[str] = None
    custom_fields: Optional[list] = None


class WebhookUpdate(BaseModel):
    url: Optional[str] = None
    events: Optional[list[str]] = None
    is_active: Optional[bool] = None
    secret: Optional[str] = None
    custom_fields: Optional[list] = None


@router.get("")
async def list_webhooks(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, page=page, per_page=per_page, search_fields=["url"])


@router.post("", status_code=201)
async def create_webhook(body: WebhookCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{webhook_id}")
async def get_webhook(webhook_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, webhook_id, user, LABEL)


@router.put("/{webhook_id}")
async def update_webhook(webhook_id: UUID, body: WebhookUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, webhook_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{webhook_id}")
async def delete_webhook(webhook_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, webhook_id, user, LABEL)
