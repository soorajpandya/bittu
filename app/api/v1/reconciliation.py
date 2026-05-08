"""
Reconciliation API.

Cross-table financial integrity scans + filterable reports.

Endpoints:
    POST  /reconciliation/runs                  Trigger a new scan
    GET   /reconciliation/runs                  Run history
    GET   /reconciliation/runs/{run_id}         Run detail (incl. discrepancies)
    GET   /reconciliation/discrepancies         Filtered discrepancy list
    POST  /reconciliation/discrepancies/{id}/resolve   Mark resolved/ack/ignored
    GET   /reconciliation/summary               Unified totals + open issue counts

All endpoints are scoped to the caller's owner_id (multi-tenant isolation).
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.logging import get_logger
from app.services.reconciliation_service import reconciliation_service

router = APIRouter(prefix="/reconciliation", tags=["Reconciliation"])
logger = get_logger(__name__)


# ── Schemas ──────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    from_date: Optional[datetime] = Field(None, description="UTC start; default = now - 7 days")
    to_date:   Optional[datetime] = Field(None, description="UTC end; default = now")
    min_order_age_minutes: int = Field(15, ge=0, le=1440)


class ResolveRequest(BaseModel):
    action: str = Field(..., pattern="^(acknowledged|resolved|ignored)$")
    notes:  Optional[str] = None


# ── Runs ─────────────────────────────────────────────────────────────────

@router.post("/runs")
async def trigger_run(
    body: RunRequest,
    user: UserContext = Depends(require_permission("bank_recon.write")),
):
    """Run a reconciliation scan over the supplied window."""
    return await reconciliation_service.run_reconciliation(
        user,
        from_date=body.from_date,
        to_date=body.to_date,
        min_order_age_minutes=body.min_order_age_minutes,
    )


@router.get("/runs")
async def list_runs(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    """Reconciliation run history (most recent first)."""
    return await reconciliation_service.list_runs(user, limit=limit, offset=offset)


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    """Run header + all discrepancies it produced."""
    return await reconciliation_service.get_run(user, run_id)


# ── Discrepancies ────────────────────────────────────────────────────────

@router.get("/discrepancies")
async def list_discrepancies(
    kind: Optional[str] = Query(None, description=
        "payment_received_order_not_updated | order_created_payment_missing | "
        "duplicate_payment | failed_settlement | partial_settlement | "
        "webhook_delayed_or_failed | amount_mismatch | orphan_settlement"),
    severity: Optional[str] = Query(None, pattern="^(info|warning|critical)$"),
    status:   Optional[str] = Query(None, pattern="^(open|acknowledged|resolved|ignored)$"),
    order_id:    Optional[str] = None,
    payment_id:  Optional[str] = None,
    customer_id: Optional[int] = None,
    from_date: Optional[datetime] = None,
    to_date:   Optional[datetime] = None,
    limit:  int = Query(50, ge=1, le=500),
    offset: int = Query(0,  ge=0),
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    """Filtered discrepancy list. All filters are optional and AND-combined."""
    return await reconciliation_service.list_discrepancies(
        user,
        kind=kind, severity=severity, status=status,
        order_id=order_id, payment_id=payment_id, customer_id=customer_id,
        from_date=from_date, to_date=to_date,
        limit=limit, offset=offset,
    )


@router.post("/discrepancies/{discrepancy_id}/resolve")
async def resolve_discrepancy(
    discrepancy_id: str,
    body: ResolveRequest,
    user: UserContext = Depends(require_permission("bank_recon.write")),
):
    """Acknowledge, resolve, or ignore an open discrepancy."""
    return await reconciliation_service.resolve_discrepancy(
        user, discrepancy_id, action=body.action, notes=body.notes,
    )


# ── Unified summary report ───────────────────────────────────────────────

@router.get("/summary")
async def summary(
    from_date: Optional[datetime] = None,
    to_date:   Optional[datetime] = None,
    user: UserContext = Depends(require_permission("bank_recon.read")),
):
    """
    Single-pane snapshot for the period:
      orders ▸ payments ▸ settlements ▸ open issues by kind ▸ webhook health.
    Default window = last 30 days.
    """
    return await reconciliation_service.summary(user, from_date=from_date, to_date=to_date)
