"""
Bank Reconciliation Engine — Merchant API (Phase 3).

Prefix:   /recon
Audience: a merchant managing their own bank statements.
All endpoints scope to ``user.restaurant_id`` — a merchant CANNOT see
another merchant's data through this router.  For cross-merchant access use
the platform-admin router at ``/admin/recon``.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Body, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.services.recon_engine_service import recon_engine_service

router = APIRouter(prefix="/recon", tags=["Reconciliation"])
logger = get_logger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────
def _merchant_id(user: UserContext) -> str:
    if not user.restaurant_id:
        raise ValidationError("Active restaurant context is required.")
    return str(user.restaurant_id)


# ── Schemas ──────────────────────────────────────────────────────────────
class AccountCreate(BaseModel):
    account_label:        str = Field(..., min_length=2, max_length=100)
    bank_name:            Optional[str] = None
    account_number_last4: Optional[str] = Field(None, max_length=4)
    ifsc:                 Optional[str] = Field(None, max_length=16)
    currency:             str = Field("INR", min_length=3, max_length=3)
    metadata:             Optional[dict] = None


class WebhookIngest(BaseModel):
    rows: list[dict] = Field(..., description="Bank lines, see ingest_rows()")
    metadata: Optional[dict] = None


class ManualMatchBody(BaseModel):
    settlement_id:   Optional[str] = None
    escrow_entry_id: Optional[str] = None


class RunRequest(BaseModel):
    account_id: Optional[str] = None
    from_date:  Optional[date] = None
    to_date:    Optional[date] = None


class DiscrepancyUpdate(BaseModel):
    status:           str = Field(..., pattern="^(open|investigating|resolved|ignored)$")
    resolution_notes: Optional[str] = None


# ── Bank accounts ────────────────────────────────────────────────────────
@router.get("/accounts")
async def list_accounts(
    only_active: bool = Query(True),
    user: UserContext = Depends(require_permission("recon.read")),
):
    return await recon_engine_service.list_accounts(
        merchant_id=_merchant_id(user), only_active=only_active,
    )


@router.post("/accounts")
async def create_account(
    body: AccountCreate,
    user: UserContext = Depends(require_permission("recon.write")),
):
    return await recon_engine_service.create_account(
        merchant_id=_merchant_id(user), **body.model_dump(),
    )


@router.delete("/accounts/{account_id}")
async def deactivate_account(
    account_id: str,
    user: UserContext = Depends(require_permission("recon.write")),
):
    return await recon_engine_service.deactivate_account(
        account_id=account_id, merchant_id=_merchant_id(user),
    )


# ── Ingest ───────────────────────────────────────────────────────────────
@router.post("/accounts/{account_id}/import-csv")
async def import_csv(
    account_id: str,
    file: UploadFile = File(...),
    user: UserContext = Depends(require_permission("recon.write")),
):
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return await recon_engine_service.ingest_csv(
        merchant_id=_merchant_id(user),
        account_id=account_id,
        csv_text=text,
        original_filename=file.filename,
        imported_by=user.user_id,
    )


@router.post("/accounts/{account_id}/webhook")
async def import_webhook(
    account_id: str,
    body: WebhookIngest,
    user: UserContext = Depends(require_permission("recon.write")),
):
    return await recon_engine_service.ingest_rows(
        merchant_id=_merchant_id(user),
        account_id=account_id,
        rows=body.rows,
        source="webhook",
        metadata=body.metadata,
        imported_by=user.user_id,
    )


@router.get("/imports")
async def list_imports(
    account_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(require_permission("recon.read")),
):
    return await recon_engine_service.list_imports(
        merchant_id=_merchant_id(user), account_id=account_id, limit=limit,
    )


# ── Lines ────────────────────────────────────────────────────────────────
@router.get("/lines")
async def list_lines(
    account_id:   Optional[str] = Query(None),
    match_status: Optional[str] = Query(None,
        description="unmatched|matched|partial|ignored"),
    from_date:    Optional[date] = Query(None),
    to_date:      Optional[date] = Query(None),
    limit:        int = Query(50, ge=1, le=200),
    cursor:       Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("recon.read")),
):
    return await recon_engine_service.list_lines(
        merchant_id=_merchant_id(user),
        account_id=account_id, match_status=match_status,
        from_date=from_date, to_date=to_date,
        limit=limit, cursor=cursor,
    )


@router.get("/lines/{line_id}")
async def get_line(
    line_id: str,
    user: UserContext = Depends(require_permission("recon.read")),
):
    return await recon_engine_service.get_line(
        line_id=line_id, merchant_id=_merchant_id(user),
    )


@router.post("/lines/{line_id}/match")
async def match_line(
    line_id: str,
    body: ManualMatchBody,
    user: UserContext = Depends(require_permission("recon.write")),
):
    return await recon_engine_service.manual_match(
        line_id=line_id,
        actor_id=user.user_id,
        merchant_id=_merchant_id(user),
        settlement_id=body.settlement_id,
        escrow_entry_id=body.escrow_entry_id,
    )


@router.post("/lines/{line_id}/unmatch")
async def unmatch_line(
    line_id: str,
    user: UserContext = Depends(require_permission("recon.write")),
):
    return await recon_engine_service.unmatch_line(
        line_id=line_id, actor_id=user.user_id, merchant_id=_merchant_id(user),
    )


# ── Match runs ───────────────────────────────────────────────────────────
@router.post("/run")
async def run_match(
    body: RunRequest = Body(default_factory=RunRequest),
    user: UserContext = Depends(require_permission("recon.write")),
):
    return await recon_engine_service.run_match_engine(
        merchant_id=_merchant_id(user),
        account_id=body.account_id,
        scope_from=body.from_date,
        scope_to=body.to_date,
        triggered_by=user.user_id,
        is_admin_run=False,
    )


@router.get("/runs")
async def list_runs(
    limit: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(require_permission("recon.read")),
):
    return await recon_engine_service.list_runs(
        merchant_id=_merchant_id(user), limit=limit,
    )


# ── Discrepancies ────────────────────────────────────────────────────────
@router.get("/discrepancies")
async def list_discrepancies(
    kind:      Optional[str] = Query(None),
    status:    Optional[str] = Query(None),
    severity:  Optional[str] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    limit:     int = Query(50, ge=1, le=200),
    cursor:    Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("recon.read")),
):
    return await recon_engine_service.list_discrepancies(
        merchant_id=_merchant_id(user),
        kind=kind, status=status, severity=severity,
        from_date=from_date, to_date=to_date,
        limit=limit, cursor=cursor,
    )


@router.patch("/discrepancies/{discrepancy_id}")
async def update_discrepancy(
    discrepancy_id: str,
    body: DiscrepancyUpdate,
    user: UserContext = Depends(require_permission("recon.write")),
):
    return await recon_engine_service.resolve_discrepancy(
        discrepancy_id=discrepancy_id,
        actor_id=user.user_id,
        merchant_id=_merchant_id(user),
        new_status=body.status,
        resolution_notes=body.resolution_notes,
    )


# ── Summary ──────────────────────────────────────────────────────────────
@router.get("/summary")
async def get_summary(
    user: UserContext = Depends(require_permission("recon.read")),
):
    return await recon_engine_service.get_summary(merchant_id=_merchant_id(user))
