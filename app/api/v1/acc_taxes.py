"""Taxes CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/taxes", tags=["Accounting – Taxes"])

TABLE = "acc_taxes"
PK = "tax_id"
LABEL = "Tax"


_auth = require_permission("accounting:read")


class TaxCreate(BaseModel):
    tax_name: str
    tax_percentage: float = 0
    tax_type: Optional[str] = None
    tax_factor: Optional[str] = None
    tax_specific_type: Optional[str] = None
    tax_authority_name: Optional[str] = None
    tax_authority_id: Optional[UUID] = None
    country_code: Optional[str] = None
    is_editable: bool = True
    is_value_added: bool = False
    purchase_tax_expense_account_id: Optional[UUID] = None


class TaxUpdate(BaseModel):
    tax_name: Optional[str] = None
    tax_percentage: Optional[float] = None
    tax_type: Optional[str] = None
    tax_factor: Optional[str] = None
    tax_specific_type: Optional[str] = None
    tax_authority_name: Optional[str] = None
    country_code: Optional[str] = None
    is_editable: Optional[bool] = None
    is_value_added: Optional[bool] = None
    status: Optional[str] = None


class TaxGroupCreate(BaseModel):
    tax_group_name: str
    tax_group_percentage: float = 0
    taxes: Optional[list] = None


class TaxGroupUpdate(BaseModel):
    tax_group_name: Optional[str] = None
    taxes: Optional[list] = None


class TaxAuthorityCreate(BaseModel):
    tax_authority_name: str
    description: Optional[str] = None


class TaxAuthorityUpdate(BaseModel):
    tax_authority_name: Optional[str] = None
    description: Optional[str] = None


class TaxExemptionCreate(BaseModel):
    name: str
    tax_exemption_code: Optional[str] = None
    description: Optional[str] = None
    exemption_type: Optional[str] = None


class TaxExemptionUpdate(BaseModel):
    name: Optional[str] = None
    tax_exemption_code: Optional[str] = None
    description: Optional[str] = None
    exemption_type: Optional[str] = None


@router.get("")
async def list_taxes(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, page=page, per_page=per_page)


@router.post("")
async def create_tax(body: TaxCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{tax_id}")
async def get_tax(tax_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, tax_id, user, LABEL)


@router.put("/{tax_id}")
async def update_tax(tax_id: UUID, body: TaxUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, tax_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{tax_id}")
async def delete_tax(tax_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, tax_id, user, LABEL)


# ── Tax Groups ──
@router.get("/groups/{tax_group_id}")
async def get_tax_group(tax_group_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get("acc_tax_groups", "tax_group_id", tax_group_id, user, "Tax Group")


@router.post("/groups")
async def create_tax_group(body: TaxGroupCreate, user: UserContext = Depends(_auth)):
    return await acc_create("acc_tax_groups", body.model_dump(exclude_none=True), user)


@router.put("/groups/{tax_group_id}")
async def update_tax_group(tax_group_id: UUID, body: TaxGroupUpdate, user: UserContext = Depends(_auth)):
    return await acc_update("acc_tax_groups", "tax_group_id", tax_group_id, body.model_dump(exclude_unset=True, exclude_none=True), user, "Tax Group")


@router.delete("/groups/{tax_group_id}")
async def delete_tax_group(tax_group_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete("acc_tax_groups", "tax_group_id", tax_group_id, user, "Tax Group")


# ── Tax Authorities ──
@router.get("/settings/taxauthorities")
async def list_tax_authorities(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list("acc_tax_authorities", user, page=page, per_page=per_page)


@router.post("/settings/taxauthorities")
async def create_tax_authority(body: TaxAuthorityCreate, user: UserContext = Depends(_auth)):
    return await acc_create("acc_tax_authorities", body.model_dump(exclude_none=True), user)


@router.get("/settings/taxauthorities/{tax_authority_id}")
async def get_tax_authority(tax_authority_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get("acc_tax_authorities", "tax_authority_id", tax_authority_id, user, "Tax Authority")


@router.put("/settings/taxauthorities/{tax_authority_id}")
async def update_tax_authority(tax_authority_id: UUID, body: TaxAuthorityUpdate, user: UserContext = Depends(_auth)):
    return await acc_update("acc_tax_authorities", "tax_authority_id", tax_authority_id, body.model_dump(exclude_unset=True, exclude_none=True), user, "Tax Authority")


@router.delete("/settings/taxauthorities/{tax_authority_id}")
async def delete_tax_authority(tax_authority_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete("acc_tax_authorities", "tax_authority_id", tax_authority_id, user, "Tax Authority")


# ── Tax Exemptions ──
@router.get("/settings/taxexemptions")
async def list_tax_exemptions(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list("acc_tax_exemptions", user, page=page, per_page=per_page)


@router.post("/settings/taxexemptions")
async def create_tax_exemption(body: TaxExemptionCreate, user: UserContext = Depends(_auth)):
    return await acc_create("acc_tax_exemptions", body.model_dump(exclude_none=True), user)


@router.get("/settings/taxexemptions/{tax_exemption_id}")
async def get_tax_exemption(tax_exemption_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get("acc_tax_exemptions", "tax_exemption_id", tax_exemption_id, user, "Tax Exemption")


@router.put("/settings/taxexemptions/{tax_exemption_id}")
async def update_tax_exemption(tax_exemption_id: UUID, body: TaxExemptionUpdate, user: UserContext = Depends(_auth)):
    return await acc_update("acc_tax_exemptions", "tax_exemption_id", tax_exemption_id, body.model_dump(exclude_unset=True, exclude_none=True), user, "Tax Exemption")


@router.delete("/settings/taxexemptions/{tax_exemption_id}")
async def delete_tax_exemption(tax_exemption_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete("acc_tax_exemptions", "tax_exemption_id", tax_exemption_id, user, "Tax Exemption")
