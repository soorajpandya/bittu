"""Bank Accounts CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/bankaccounts", tags=["Accounting – Bank Accounts"])

TABLE = "acc_bank_accounts"
PK = "account_id"
LABEL = "Bank Account"


_auth = require_permission("accounting:read")


class BankAccountCreate(BaseModel):
    account_name: str
    account_type: str = "bank"
    account_number: Optional[str] = None
    bank_name: Optional[str] = None
    routing_number: Optional[str] = None
    currency_code: Optional[str] = None
    description: Optional[str] = None
    is_primary_account: bool = False
    paypal_email_address: Optional[str] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


class BankAccountUpdate(BaseModel):
    account_name: Optional[str] = None
    account_type: Optional[str] = None
    account_number: Optional[str] = None
    bank_name: Optional[str] = None
    routing_number: Optional[str] = None
    currency_code: Optional[str] = None
    description: Optional[str] = None
    is_primary_account: Optional[bool] = None
    paypal_email_address: Optional[str] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


@router.get("")
async def list_bank_accounts(
    user: UserContext = Depends(_auth),
    account_type: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"account_type": account_type}, page=page, per_page=per_page, search_fields=["account_name", "bank_name"])


@router.post("", status_code=201)
async def create_bank_account(body: BankAccountCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{account_id}")
async def get_bank_account(account_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, account_id, user, LABEL)


@router.put("/{account_id}")
async def update_bank_account(account_id: UUID, body: BankAccountUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, account_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{account_id}")
async def delete_bank_account(account_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, account_id, user, LABEL)
