"""Audit Log endpoints (read-only)."""
from typing import Optional
from fastapi import APIRouter, Depends, Query

from app.core.auth import UserContext, require_role
from app.services.misc_service import AuditService

router = APIRouter(prefix="/audit-logs", tags=["Audit Logs"])
_svc = AuditService()


@router.get("")
async def list_audit_logs(
    entity_type: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.list_audit_logs(user, entity_type=entity_type, limit=limit, offset=offset)
