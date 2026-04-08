"""Bank Rules CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/bankrules", tags=["Accounting – Bank Rules"])

TABLE = "acc_bank_rules"
PK = "rule_id"
LABEL = "Bank Rule"


_auth = require_permission("accounting:read")


class RuleCriterion(BaseModel):
    field: str
    comparator: str
    value: str


class BankRuleCreate(BaseModel):
    rule_name: str
    rule_order: int = 0
    apply_to: str = "withdrawals"
    criteria_type: str = "and"
    criterion: Optional[list[RuleCriterion]] = None
    record_as: str = "expense"
    account_id: Optional[UUID] = None
    tax_id: Optional[UUID] = None
    customer_id: Optional[UUID] = None
    vendor_id: Optional[UUID] = None
    reference_number: Optional[str] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


class BankRuleUpdate(BaseModel):
    rule_name: Optional[str] = None
    rule_order: Optional[int] = None
    apply_to: Optional[str] = None
    criteria_type: Optional[str] = None
    criterion: Optional[list[RuleCriterion]] = None
    record_as: Optional[str] = None
    account_id: Optional[UUID] = None
    tax_id: Optional[UUID] = None
    customer_id: Optional[UUID] = None
    vendor_id: Optional[UUID] = None
    reference_number: Optional[str] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


@router.get("")
async def list_bank_rules(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, page=page, per_page=per_page, order_by="rule_order ASC", search_fields=["rule_name"])


@router.post("", status_code=201)
async def create_bank_rule(body: BankRuleCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    if data.get("criterion"):
        data["criterion"] = [c.model_dump() if hasattr(c, "model_dump") else c for c in data["criterion"]]
    return await acc_create(TABLE, data, user)


@router.get("/{rule_id}")
async def get_bank_rule(rule_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, rule_id, user, LABEL)


@router.put("/{rule_id}")
async def update_bank_rule(rule_id: UUID, body: BankRuleUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    if data.get("criterion"):
        data["criterion"] = [c.model_dump() if hasattr(c, "model_dump") else c for c in data["criterion"]]
    return await acc_update(TABLE, PK, rule_id, data, user, LABEL)


@router.delete("/{rule_id}")
async def delete_bank_rule(rule_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, rule_id, user, LABEL)
