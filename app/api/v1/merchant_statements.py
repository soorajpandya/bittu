"""
Merchant Statements — Merchant API (Phase 5).

Prefix:   /merchant-statements
Audience: a merchant generating/viewing their own period statements over
the merchant_ledger.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query, Response
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.services.merchant_statement_service import merchant_statement_service

router = APIRouter(prefix="/merchant-statements", tags=["Statements"])
logger = get_logger(__name__)


def _merchant_id(user: UserContext) -> str:
    if not user.restaurant_id:
        raise ValidationError("Active restaurant context is required.")
    return str(user.restaurant_id)


class GenerateBody(BaseModel):
    period_start: datetime
    period_end:   datetime
    currency:     str = Field("INR", min_length=3, max_length=3)
    notes:        Optional[str] = None
    metadata:     Optional[dict] = None


@router.post("/", status_code=201)
async def generate_statement(
    body: GenerateBody,
    user: UserContext = Depends(require_permission("statement.generate")),
):
    return await merchant_statement_service.generate(
        merchant_id=_merchant_id(user),
        period_start=body.period_start,
        period_end=body.period_end,
        currency=body.currency,
        generated_by=user.user_id,
        notes=body.notes,
        metadata=body.metadata,
    )


@router.get("/")
async def list_statements(
    status:    Optional[str] = Query(None, pattern=r"^(generating|ready|cancelled)$"),
    from_date: Optional[datetime] = Query(None),
    to_date:   Optional[datetime] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    user: UserContext = Depends(require_permission("statement.read")),
):
    return await merchant_statement_service.list_statements(
        merchant_id=_merchant_id(user),
        status=status, from_date=from_date, to_date=to_date, limit=limit,
    )


@router.get("/{statement_id}")
async def get_statement(
    statement_id: str,
    user: UserContext = Depends(require_permission("statement.read")),
):
    return await merchant_statement_service.get_statement(
        statement_id=statement_id, merchant_id=_merchant_id(user),
    )


@router.get("/{statement_id}/entries")
async def list_entries(
    statement_id: str,
    limit: int = Query(1000, ge=1, le=5000),
    user: UserContext = Depends(require_permission("statement.read")),
):
    return await merchant_statement_service.list_entries(
        statement_id=statement_id, merchant_id=_merchant_id(user), limit=limit,
    )


@router.get("/{statement_id}/csv")
async def download_csv(
    statement_id: str,
    download: bool = Query(True),
    user: UserContext = Depends(require_permission("statement.read")),
):
    out = await merchant_statement_service.to_csv(
        statement_id=statement_id, merchant_id=_merchant_id(user),
    )
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
