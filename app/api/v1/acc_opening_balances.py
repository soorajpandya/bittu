"""Opening Balances CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/openingbalances", tags=["Accounting – Opening Balances"])

TABLE = "acc_opening_balances"
PK = "opening_balance_id"
LABEL = "Opening Balance"


_auth = require_permission("accounting:read")


class OpeningBalanceCreate(BaseModel):
    date: Optional[str] = None
    accounts: Optional[list[dict]] = None  # [{account_id, debit, credit}]
    custom_fields: Optional[list] = None


class OpeningBalanceUpdate(BaseModel):
    date: Optional[str] = None
    accounts: Optional[list[dict]] = None
    custom_fields: Optional[list] = None


@router.get("")
async def list_opening_balances(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, page=page, per_page=per_page)


@router.post("", status_code=201)
async def create_opening_balance(body: OpeningBalanceCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{opening_balance_id}")
async def get_opening_balance(opening_balance_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, opening_balance_id, user, LABEL)


@router.put("/{opening_balance_id}")
async def update_opening_balance(opening_balance_id: UUID, body: OpeningBalanceUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, opening_balance_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{opening_balance_id}")
async def delete_opening_balance(opening_balance_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, opening_balance_id, user, LABEL)
