"""Chart of Accounts CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/chartofaccounts", tags=["Accounting – Chart of Accounts"])

TABLE = "acc_chart_of_accounts"
PK = "account_id"
LABEL = "Account"


_auth = require_permission("accounting:read")


class AccountCreate(BaseModel):
    account_name: str
    account_code: Optional[str] = None
    account_type: str  # income, expense, equity, asset, liability
    currency_id: Optional[UUID] = None
    description: Optional[str] = None
    show_on_dashboard: bool = False
    include_in_vat_return: bool = False
    parent_account_id: Optional[UUID] = None
    custom_fields: Optional[list] = None


class AccountUpdate(BaseModel):
    account_name: Optional[str] = None
    account_code: Optional[str] = None
    account_type: Optional[str] = None
    currency_id: Optional[UUID] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    show_on_dashboard: Optional[bool] = None
    include_in_vat_return: Optional[bool] = None
    parent_account_id: Optional[UUID] = None
    custom_fields: Optional[list] = None


@router.get("")
async def list_accounts(
    user: UserContext = Depends(_auth),
    account_type: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"account_type": account_type, "is_active": is_active}, page=page, per_page=per_page)


@router.post("")
async def create_account(body: AccountCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{account_id}")
async def get_account(account_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, account_id, user, LABEL)


@router.put("/{account_id}")
async def update_account(account_id: UUID, body: AccountUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, account_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{account_id}")
async def delete_account(account_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, account_id, user, LABEL)
