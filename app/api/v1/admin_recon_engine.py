"""
Bank Reconciliation Engine — Platform-Admin API (Phase 3).

Prefix:   /admin/recon
Audience: platform admins (rows in ``platform_admin_users``).
Every endpoint is gated by ``require_platform_admin()``.

Admins may filter by ``merchant_id`` to drill into a single merchant; if
no filter is supplied, results span ALL merchants on the platform.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Body, Depends, File, Query, UploadFile
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_platform_admin
from app.core.logging import get_logger
from app.services.recon_engine_service import recon_engine_service

router = APIRouter(prefix="/admin/recon", tags=["Reconciliation (Admin)"])
logger = get_logger(__name__)


# ── Schemas ──────────────────────────────────────────────────────────────
class AdminAccountCreate(BaseModel):
    merchant_id:          str
    account_label:        str = Field(..., min_length=2, max_length=100)
    bank_name:            Optional[str] = None
    account_number_last4: Optional[str] = Field(None, max_length=4)
    ifsc:                 Optional[str] = Field(None, max_length=16)
    currency:             str = Field("INR", min_length=3, max_length=3)
    metadata:             Optional[dict] = None


class AdminRunRequest(BaseModel):
    merchant_id: Optional[str] = Field(None, description="None ⇒ run across all merchants")
    account_id:  Optional[str] = None
    from_date:   Optional[date] = None
    to_date:     Optional[date] = None


class AdminDiscrepancyUpdate(BaseModel):
    status:           str = Field(..., pattern="^(open|investigating|resolved|ignored)$")
    resolution_notes: Optional[str] = None


class PlatformAdminCreate(BaseModel):
    user_id: str
    email:   Optional[str] = None
    notes:   Optional[str] = None


# ── Bank accounts (cross-merchant) ───────────────────────────────────────
@router.get("/accounts")
async def list_accounts(
    merchant_id: Optional[str] = Query(None),
    only_active: bool = Query(False),
    _: UserContext = Depends(require_platform_admin()),
):
    return await recon_engine_service.list_accounts(
        merchant_id=merchant_id, only_active=only_active,
    )


@router.post("/accounts")
async def create_account(
    body: AdminAccountCreate,
    _: UserContext = Depends(require_platform_admin()),
):
    return await recon_engine_service.create_account(**body.model_dump())


@router.delete("/accounts/{account_id}")
async def deactivate_account(
    account_id: str,
    _: UserContext = Depends(require_platform_admin()),
):
    return await recon_engine_service.deactivate_account(account_id=account_id)


# ── Ingest on behalf of a merchant ───────────────────────────────────────
@router.post("/accounts/{account_id}/import-csv")
async def admin_import_csv(
    account_id: str,
    merchant_id: str = Query(..., description="Merchant that owns the account"),
    file: UploadFile = File(...),
    user: UserContext = Depends(require_platform_admin()),
):
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return await recon_engine_service.ingest_csv(
        merchant_id=merchant_id, account_id=account_id,
        csv_text=text, original_filename=file.filename,
        imported_by=user.user_id,
    )


# ── Lines / runs / discrepancies (cross-merchant) ────────────────────────
@router.get("/lines")
async def admin_list_lines(
    merchant_id:  Optional[str] = Query(None),
    account_id:   Optional[str] = Query(None),
    match_status: Optional[str] = Query(None),
    from_date:    Optional[date] = Query(None),
    to_date:      Optional[date] = Query(None),
    limit:        int = Query(50, ge=1, le=200),
    cursor:       Optional[str] = Query(None),
    _: UserContext = Depends(require_platform_admin()),
):
    return await recon_engine_service.list_lines(
        merchant_id=merchant_id, account_id=account_id,
        match_status=match_status, from_date=from_date, to_date=to_date,
        limit=limit, cursor=cursor,
    )


@router.get("/imports")
async def admin_list_imports(
    merchant_id: Optional[str] = Query(None),
    account_id:  Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    _: UserContext = Depends(require_platform_admin()),
):
    return await recon_engine_service.list_imports(
        merchant_id=merchant_id, account_id=account_id, limit=limit,
    )


@router.post("/run")
async def admin_run_match(
    body: AdminRunRequest = Body(default_factory=AdminRunRequest),
    user: UserContext = Depends(require_platform_admin()),
):
    return await recon_engine_service.run_match_engine(
        merchant_id=body.merchant_id,
        account_id=body.account_id,
        scope_from=body.from_date,
        scope_to=body.to_date,
        triggered_by=user.user_id,
        is_admin_run=True,
    )


@router.get("/runs")
async def admin_list_runs(
    merchant_id:  Optional[str] = Query(None),
    is_admin_run: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    _: UserContext = Depends(require_platform_admin()),
):
    return await recon_engine_service.list_runs(
        merchant_id=merchant_id, is_admin_run=is_admin_run, limit=limit,
    )


@router.get("/discrepancies")
async def admin_list_discrepancies(
    merchant_id: Optional[str] = Query(None),
    kind:        Optional[str] = Query(None),
    status:      Optional[str] = Query(None),
    severity:    Optional[str] = Query(None),
    from_date:   Optional[date] = Query(None),
    to_date:     Optional[date] = Query(None),
    limit:       int = Query(50, ge=1, le=200),
    cursor:      Optional[str] = Query(None),
    _: UserContext = Depends(require_platform_admin()),
):
    return await recon_engine_service.list_discrepancies(
        merchant_id=merchant_id, kind=kind, status=status, severity=severity,
        from_date=from_date, to_date=to_date, limit=limit, cursor=cursor,
    )


@router.patch("/discrepancies/{discrepancy_id}")
async def admin_update_discrepancy(
    discrepancy_id: str,
    body: AdminDiscrepancyUpdate,
    user: UserContext = Depends(require_platform_admin()),
):
    return await recon_engine_service.resolve_discrepancy(
        discrepancy_id=discrepancy_id, actor_id=user.user_id,
        new_status=body.status, resolution_notes=body.resolution_notes,
    )


# ── Aggregates ───────────────────────────────────────────────────────────
@router.get("/summary")
async def admin_summary(
    merchant_id: Optional[str] = Query(None),
    _: UserContext = Depends(require_platform_admin()),
):
    """Global summary, or per-merchant if ``merchant_id`` provided."""
    return await recon_engine_service.get_summary(merchant_id=merchant_id)


@router.get("/summary/by-merchant")
async def admin_summary_by_merchant(
    _: UserContext = Depends(require_platform_admin()),
):
    """Per-merchant rollup of line counts + open discrepancies."""
    return await recon_engine_service.admin_summary_by_merchant()


# ── Platform-admin membership management ─────────────────────────────────
@router.get("/platform-admins")
async def list_platform_admins(
    _: UserContext = Depends(require_platform_admin()),
):
    return await recon_engine_service.list_platform_admins()


@router.post("/platform-admins")
async def add_platform_admin(
    body: PlatformAdminCreate,
    user: UserContext = Depends(require_platform_admin()),
):
    return await recon_engine_service.add_platform_admin(
        user_id=body.user_id, email=body.email, notes=body.notes,
        created_by=user.user_id,
    )


@router.delete("/platform-admins/{user_id}")
async def remove_platform_admin(
    user_id: str,
    _: UserContext = Depends(require_platform_admin()),
):
    removed = await recon_engine_service.remove_platform_admin(user_id=user_id)
    return {"removed": removed}
