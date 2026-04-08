"""Currencies CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update

router = APIRouter(prefix="/accounting/currencies", tags=["Accounting – Currencies"])

TABLE = "acc_currencies"
PK = "currency_id"
LABEL = "Currency"


_auth = require_permission("accounting:read")


class CurrencyCreate(BaseModel):
    currency_code: str
    currency_name: Optional[str] = None
    currency_symbol: Optional[str] = None
    price_precision: int = 2
    currency_format: Optional[str] = None
    is_base_currency: bool = False
    exchange_rate: float = 1.0
    effective_date: Optional[str] = None


class CurrencyUpdate(BaseModel):
    currency_code: Optional[str] = None
    currency_name: Optional[str] = None
    currency_symbol: Optional[str] = None
    price_precision: Optional[int] = None
    currency_format: Optional[str] = None
    exchange_rate: Optional[float] = None
    effective_date: Optional[str] = None


@router.get("")
async def list_currencies(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, page=page, per_page=per_page)


@router.post("")
async def create_currency(body: CurrencyCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{currency_id}")
async def get_currency(currency_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, currency_id, user, LABEL)


@router.put("/{currency_id}")
async def update_currency(currency_id: UUID, body: CurrencyUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, currency_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)
