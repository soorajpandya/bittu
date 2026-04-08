"""Accounting Items CRUD endpoints (separate from menu items)."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete, acc_status_update

router = APIRouter(prefix="/accounting/items", tags=["Accounting – Items"])

TABLE = "acc_items"
PK = "item_id"
LABEL = "Item"


_auth = require_permission("accounting:read")


class ItemCreate(BaseModel):
    name: str
    description: Optional[str] = None
    rate: float = 0
    unit: Optional[str] = None
    sku: Optional[str] = None
    item_type: str = "sales"
    product_type: str = "goods"
    is_taxable: bool = True
    tax_id: Optional[UUID] = None
    account_id: Optional[UUID] = None
    purchase_account_id: Optional[UUID] = None
    inventory_account_id: Optional[UUID] = None
    purchase_rate: float = 0
    purchase_description: Optional[str] = None
    initial_stock: float = 0
    initial_stock_rate: float = 0
    reorder_level: float = 0
    vendor_id: Optional[UUID] = None
    hsn_or_sac: Optional[str] = None
    status: str = "active"
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


class ItemUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    rate: Optional[float] = None
    unit: Optional[str] = None
    sku: Optional[str] = None
    item_type: Optional[str] = None
    product_type: Optional[str] = None
    is_taxable: Optional[bool] = None
    tax_id: Optional[UUID] = None
    account_id: Optional[UUID] = None
    purchase_account_id: Optional[UUID] = None
    inventory_account_id: Optional[UUID] = None
    purchase_rate: Optional[float] = None
    purchase_description: Optional[str] = None
    reorder_level: Optional[float] = None
    vendor_id: Optional[UUID] = None
    hsn_or_sac: Optional[str] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


@router.get("")
async def list_items(
    user: UserContext = Depends(_auth),
    item_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"item_type": item_type, "status": status}, page=page, per_page=per_page, search_fields=["name", "sku", "description"])


@router.post("", status_code=201)
async def create_item(body: ItemCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{item_id}")
async def get_item(item_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, item_id, user, LABEL)


@router.put("/{item_id}")
async def update_item(item_id: UUID, body: ItemUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, item_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{item_id}")
async def delete_item(item_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, item_id, user, LABEL)


@router.post("/{item_id}/active")
async def mark_active(item_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, item_id, "active", user, LABEL)


@router.post("/{item_id}/inactive")
async def mark_inactive(item_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, item_id, "inactive", user, LABEL)
