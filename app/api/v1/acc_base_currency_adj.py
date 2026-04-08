"""Base Currency Adjustment CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_delete

router = APIRouter(prefix="/accounting/basecurrencyadjustment", tags=["Accounting – Base Currency Adjustment"])

TABLE = "acc_base_currency_adjustments"
PK = "base_currency_adjustment_id"
LABEL = "Base Currency Adjustment"


_auth = require_permission("accounting:read")


class CurrencyAdjCreate(BaseModel):
    adjustment_date: Optional[str] = None
    currency_code: str
    exchange_rate: float
    notes: Optional[str] = None
    custom_fields: Optional[list] = None


@router.get("")
async def list_adjustments(
    user: UserContext = Depends(_auth),
    currency_code: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"currency_code": currency_code}, page=page, per_page=per_page, order_by="adjustment_date DESC")


@router.post("", status_code=201)
async def create_adjustment(body: CurrencyAdjCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{base_currency_adjustment_id}")
async def get_adjustment(base_currency_adjustment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, base_currency_adjustment_id, user, LABEL)


@router.delete("/{base_currency_adjustment_id}")
async def delete_adjustment(base_currency_adjustment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, base_currency_adjustment_id, user, LABEL)
