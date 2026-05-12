"""
Merchant Statements — Admin API (Phase 5).

Prefix:   /admin/merchant-statements
Audience: platform admins (membership in ``platform_admin_users``).
Every endpoint is gated by :func:`require_platform_admin`.
Cross-merchant: admin can generate / list / cancel for any merchant.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_platform_admin
from app.core.logging import get_logger
from app.services.merchant_statement_service import merchant_statement_service

router = APIRouter(prefix="/admin/merchant-statements",
                   tags=["Merchant Statements (Admin)"])
logger = get_logger(__name__)


class GenerateBody(BaseModel):
    merchant_id:  str
    period_start: datetime
    period_end:   datetime
    currency:     str = Field("INR", min_length=3, max_length=3)
    notes:        Optional[str] = None
    metadata:     Optional[dict] = None


class RejectBody(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)


@router.post("/", status_code=201)
async def generate_statement(
    body: GenerateBody,
    user: UserContext = Depends(require_platform_admin()),
):
    return await merchant_statement_service.generate(
        merchant_id=body.merchant_id,
        period_start=body.period_start,
        period_end=body.period_end,
        currency=body.currency,
        generated_by=user.user_id,
        notes=body.notes, metadata=body.metadata,
    )


@router.get("/")
async def list_statements(
    merchant_id: Optional[str] = Query(None),
    status:      Optional[str] = Query(None, pattern=r"^(generating|ready|cancelled)$"),
    from_date:   Optional[datetime] = Query(None),
    to_date:     Optional[datetime] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    user: UserContext = Depends(require_platform_admin()),
):
    return await merchant_statement_service.list_statements(
        merchant_id=merchant_id, status=status,
        from_date=from_date, to_date=to_date, limit=limit,
    )


@router.get("/{statement_id}")
async def get_statement(
    statement_id: str,
    user: UserContext = Depends(require_platform_admin()),
):
    return await merchant_statement_service.get_statement(
        statement_id=statement_id,
    )


@router.get("/{statement_id}/entries")
async def list_entries(
    statement_id: str,
    limit: int = Query(1000, ge=1, le=5000),
    user: UserContext = Depends(require_platform_admin()),
):
    return await merchant_statement_service.list_entries(
        statement_id=statement_id, limit=limit,
    )


@router.get("/{statement_id}/csv")
async def download_csv(
    statement_id: str,
    download: bool = Query(True),
    user: UserContext = Depends(require_platform_admin()),
):
    out = await merchant_statement_service.to_csv(statement_id=statement_id)
    if download:
        return Response(
            content=out["file_content"],
            media_type="text/csv",
            headers={
                "Content-Disposition":
                    f'attachment; filename="{out["file_name"]}"',
            },
        )
    return out


@router.post("/{statement_id}/cancel")
async def cancel_statement(
    statement_id: str,
    body: RejectBody,
    user: UserContext = Depends(require_platform_admin()),
):
    return await merchant_statement_service.cancel(
        statement_id=statement_id, actor_id=user.user_id, reason=body.reason,
    )
