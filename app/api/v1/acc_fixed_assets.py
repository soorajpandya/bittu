"""Fixed Assets CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete, acc_status_update, acc_comments_list, acc_comment_add, acc_comment_delete

router = APIRouter(prefix="/accounting/fixedassets", tags=["Accounting – Fixed Assets"])

TABLE = "acc_fixed_assets"
PK = "asset_id"
LABEL = "Fixed Asset"


_auth = require_permission("accounting:read")


class FixedAssetCreate(BaseModel):
    name: str
    description: Optional[str] = None
    acquisition_date: Optional[str] = None
    acquisition_cost: float = 0
    residual_value: float = 0
    useful_life_in_months: int = 12
    depreciation_method: str = "straight_line"
    depreciation_start_date: Optional[str] = None
    asset_account_id: Optional[UUID] = None
    accumulated_depreciation_account_id: Optional[UUID] = None
    depreciation_expense_account_id: Optional[UUID] = None
    serial_number: Optional[str] = None
    location_id: Optional[UUID] = None
    status: str = "active"
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


class FixedAssetUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    acquisition_date: Optional[str] = None
    acquisition_cost: Optional[float] = None
    residual_value: Optional[float] = None
    useful_life_in_months: Optional[int] = None
    depreciation_method: Optional[str] = None
    depreciation_start_date: Optional[str] = None
    asset_account_id: Optional[UUID] = None
    accumulated_depreciation_account_id: Optional[UUID] = None
    depreciation_expense_account_id: Optional[UUID] = None
    serial_number: Optional[str] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


class WriteOffInput(BaseModel):
    write_off_date: Optional[str] = None


class SellInput(BaseModel):
    sold_date: Optional[str] = None
    sold_amount: float


class CommentInput(BaseModel):
    description: str


class AssetTypeCreate(BaseModel):
    name: str
    description: Optional[str] = None
    depreciation_method: Optional[str] = None
    useful_life_years: Optional[int] = None
    asset_account_id: Optional[UUID] = None
    depreciation_account_id: Optional[UUID] = None
    accumulated_depreciation_account_id: Optional[UUID] = None


class AssetTypeUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    depreciation_method: Optional[str] = None
    useful_life_years: Optional[int] = None
    asset_account_id: Optional[UUID] = None
    depreciation_account_id: Optional[UUID] = None
    accumulated_depreciation_account_id: Optional[UUID] = None


@router.get("")
async def list_fixed_assets(
    user: UserContext = Depends(_auth),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"status": status}, page=page, per_page=per_page, search_fields=["name", "serial_number"])


@router.post("", status_code=201)
async def create_fixed_asset(body: FixedAssetCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{asset_id}")
async def get_fixed_asset(asset_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, asset_id, user, LABEL)


@router.put("/{asset_id}")
async def update_fixed_asset(asset_id: UUID, body: FixedAssetUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, asset_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{asset_id}")
async def delete_fixed_asset(asset_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, asset_id, user, LABEL)


@router.post("/{asset_id}/status/dispose")
async def dispose_asset(asset_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, asset_id, "disposed", user, LABEL)


# ── History & Forecast ──────────────────────────────────────────

@router.get("/{fixed_asset_id}/history")
async def get_asset_history(fixed_asset_id: UUID, user: UserContext = Depends(_auth)):
    row = await acc_get(TABLE, PK, fixed_asset_id, user, LABEL)
    return {"fixed_asset_id": str(fixed_asset_id), "history": row.get("history", [])}


@router.get("/{fixed_asset_id}/forecast")
async def get_asset_forecast(fixed_asset_id: UUID, user: UserContext = Depends(_auth)):
    row = await acc_get(TABLE, PK, fixed_asset_id, user, LABEL)
    return {"fixed_asset_id": str(fixed_asset_id), "forecast": row.get("forecast", [])}


# ── Status transitions ──────────────────────────────────────────

@router.post("/{fixed_asset_id}/status/active")
async def mark_active(fixed_asset_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, fixed_asset_id, "active", user, LABEL)


@router.post("/{fixed_asset_id}/status/cancel")
async def mark_cancelled(fixed_asset_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, fixed_asset_id, "cancelled", user, LABEL)


@router.post("/{fixed_asset_id}/status/draft")
async def mark_draft(fixed_asset_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, fixed_asset_id, "draft", user, LABEL)


# ── Write-off & Sell ────────────────────────────────────────────

@router.post("/{fixed_asset_id}/writeoff")
async def write_off_asset(fixed_asset_id: UUID, body: WriteOffInput, user: UserContext = Depends(_auth)):
    data = {"status": "written_off"}
    if body.write_off_date:
        data["write_off_date"] = body.write_off_date
    return await acc_update(TABLE, PK, fixed_asset_id, data, user, LABEL)


@router.post("/{fixed_asset_id}/sell")
async def sell_asset(fixed_asset_id: UUID, body: SellInput, user: UserContext = Depends(_auth)):
    data = {"status": "sold", "sold_amount": body.sold_amount}
    if body.sold_date:
        data["sold_date"] = body.sold_date
    return await acc_update(TABLE, PK, fixed_asset_id, data, user, LABEL)


# ── Comments ────────────────────────────────────────────────────

@router.post("/{fixed_asset_id}/comments", status_code=201)
async def add_comment(fixed_asset_id: UUID, body: CommentInput, user: UserContext = Depends(_auth)):
    return await acc_comment_add(TABLE, PK, fixed_asset_id, body.description, user, LABEL)


@router.delete("/{fixed_asset_id}/comments/{comment_id}")
async def delete_comment(fixed_asset_id: UUID, comment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comment_delete(TABLE, PK, fixed_asset_id, comment_id, user, LABEL)


# ── Fixed Asset Types ───────────────────────────────────────────

TYPES_TABLE = "acc_fixed_asset_types"
TYPES_PK = "fixed_asset_type_id"
TYPES_LABEL = "Fixed Asset Type"


@router.get("/fixedassettypes")
async def list_asset_types(
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TYPES_TABLE, user, page=page, per_page=per_page)


@router.post("/fixedassettypes", status_code=201)
async def create_asset_type(body: AssetTypeCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TYPES_TABLE, body.model_dump(exclude_none=True), user)


@router.put("/fixedassettypes/{fixed_asset_type_id}")
async def update_asset_type(fixed_asset_type_id: UUID, body: AssetTypeUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TYPES_TABLE, TYPES_PK, fixed_asset_type_id, body.model_dump(exclude_unset=True, exclude_none=True), user, TYPES_LABEL)


@router.delete("/fixedassettypes/{fixed_asset_type_id}")
async def delete_asset_type(fixed_asset_type_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TYPES_TABLE, TYPES_PK, fixed_asset_type_id, user, TYPES_LABEL)
