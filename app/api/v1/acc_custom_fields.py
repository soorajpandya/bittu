"""Custom Fields CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete, acc_status_update

router = APIRouter(prefix="/accounting/customfields", tags=["Accounting – Custom Fields"])

TABLE = "acc_custom_fields"
PK = "custom_field_id"
LABEL = "Custom Field"


_auth = require_permission("accounting:read")


class CustomFieldCreate(BaseModel):
    label: str
    data_type: str = "string"
    entity: str  # e.g. "invoice", "contact", "bill"
    is_active: bool = True
    is_mandatory: bool = False
    placeholder: Optional[str] = None
    show_in_all_pdf: bool = False
    options: Optional[list[str]] = None


class CustomFieldUpdate(BaseModel):
    label: Optional[str] = None
    data_type: Optional[str] = None
    entity: Optional[str] = None
    is_active: Optional[bool] = None
    is_mandatory: Optional[bool] = None
    placeholder: Optional[str] = None
    show_in_all_pdf: Optional[bool] = None
    options: Optional[list[str]] = None


class ReorderItem(BaseModel):
    field_id: UUID
    sort_order: int


class StatusUpdate(BaseModel):
    field_status: str


class DropdownUpdate(BaseModel):
    dropdown_options: list


class SyntaxCheck(BaseModel):
    formula: str


@router.get("")
async def list_custom_fields(
    user: UserContext = Depends(_auth),
    entity: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"entity": entity}, page=page, per_page=per_page, search_fields=["label"])


@router.post("", status_code=201)
async def create_custom_field(body: CustomFieldCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{custom_field_id}")
async def get_custom_field(custom_field_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, custom_field_id, user, LABEL)


@router.put("/{custom_field_id}")
async def update_custom_field(custom_field_id: UUID, body: CustomFieldUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, custom_field_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{custom_field_id}")
async def delete_custom_field(custom_field_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, custom_field_id, user, LABEL)


# ── Additional Endpoints ──────────────────────────────────────────────


@router.post("/settings/fields/reorder")
async def reorder_custom_fields(items: list[ReorderItem], user: UserContext = Depends(_auth)):
    results = []
    for item in items:
        row = await acc_update(
            TABLE, PK, item.field_id, {"sort_order": item.sort_order}, user, LABEL
        )
        results.append(row)
    return {"message": "Fields reordered", "fields": results}


@router.put("/settings/fields/{field_id}/status")
async def update_field_status(field_id: UUID, body: StatusUpdate, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, field_id, body.field_status, user, LABEL)


@router.put("/settings/fields/{field_id}/dropdownoptions")
async def update_dropdown_options(field_id: UUID, body: DropdownUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(
        TABLE, PK, field_id, {"options": body.dropdown_options}, user, LABEL
    )


@router.get("/settings/fields/bulkfetch")
async def bulk_fetch_fields(
    user: UserContext = Depends(_auth),
    module: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(200, ge=1, le=500),
):
    filters = {}
    if module:
        filters["entity"] = module
    return await acc_list(TABLE, user, filters=filters, page=page, per_page=per_page, search_fields=["label"])


@router.get("/settings/fields/{field_id}/usage")
async def get_field_usage(field_id: UUID, user: UserContext = Depends(_auth)):
    await acc_get(TABLE, PK, field_id, user, LABEL)
    return {"field_id": str(field_id), "usage": {"invoices": 0, "contacts": 0, "bills": 0}}


@router.post("/settings/fields/syntax")
async def check_formula_syntax(body: SyntaxCheck, _: UserContext = Depends(_auth)):
    formula = body.formula.strip()
    valid = bool(formula) and all(ch not in formula for ch in [";", "--", "/*"])
    return {"formula": formula, "valid": valid, "message": "OK" if valid else "Invalid formula syntax"}


@router.get("/settings/fields/lookupfields")
async def list_lookup_fields(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    return await acc_list(
        TABLE, user, filters={"data_type": "lookup"}, page=page, per_page=per_page, search_fields=["label"]
    )


@router.get("/list")
async def list_custom_fields_simple(
    user: UserContext = Depends(_auth),
    entity: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"entity": entity}, page=page, per_page=per_page, search_fields=["label"])


@router.get("/fields/meta")
async def get_fields_metadata(_: UserContext = Depends(_auth)):
    return {
        "table": TABLE,
        "primary_key": PK,
        "label": LABEL,
        "supported_data_types": ["string", "number", "date", "boolean", "dropdown", "lookup", "formula"],
        "supported_entities": ["invoice", "contact", "bill", "expense", "purchase_order", "sales_order"],
    }
