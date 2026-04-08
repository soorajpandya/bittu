"""Organizations CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update

router = APIRouter(prefix="/accounting/organizations", tags=["Accounting – Organizations"])

TABLE = "acc_organizations"
PK = "organization_id"
LABEL = "Organization"


_auth = require_permission("accounting:read")


class OrgCreate(BaseModel):
    name: str
    industry_type: Optional[str] = None
    industry_size: Optional[str] = None
    fiscal_year_start_month: int = 1
    currency_code: Optional[str] = None
    time_zone: Optional[str] = None
    date_format: Optional[str] = None
    address: Optional[dict] = None
    phone: Optional[str] = None
    fax: Optional[str] = None
    website: Optional[str] = None
    email: Optional[str] = None
    tax_id_label: Optional[str] = None
    tax_id_value: Optional[str] = None
    companyid_label: Optional[str] = None
    companyid_value: Optional[str] = None
    language_code: Optional[str] = None
    is_default_org: bool = False
    custom_fields: Optional[list] = None


class OrgUpdate(BaseModel):
    name: Optional[str] = None
    industry_type: Optional[str] = None
    industry_size: Optional[str] = None
    fiscal_year_start_month: Optional[int] = None
    currency_code: Optional[str] = None
    time_zone: Optional[str] = None
    date_format: Optional[str] = None
    address: Optional[dict] = None
    phone: Optional[str] = None
    fax: Optional[str] = None
    website: Optional[str] = None
    email: Optional[str] = None
    tax_id_label: Optional[str] = None
    tax_id_value: Optional[str] = None
    companyid_label: Optional[str] = None
    companyid_value: Optional[str] = None
    language_code: Optional[str] = None
    is_default_org: Optional[bool] = None
    custom_fields: Optional[list] = None


@router.get("")
async def list_organizations(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, page=page, per_page=per_page, search_fields=["name"])


@router.post("", status_code=201)
async def create_organization(body: OrgCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{organization_id}")
async def get_organization(organization_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, organization_id, user, LABEL)


@router.put("/{organization_id}")
async def update_organization(organization_id: UUID, body: OrgUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, organization_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)
