"""Audit, Sync, Help, and other read-only services."""
from typing import Optional
from app.core.auth import UserContext
from app.core.database import get_connection
from app.core.tenant import tenant_where_clause
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class AuditService:

    async def list_audit_logs(self, user: UserContext, entity_type: Optional[str] = None, limit: int = 50, offset: int = 0) -> list[dict]:
        clause, params = tenant_where_clause(user)
        sql = f"SELECT * FROM audit_log WHERE {clause}"
        if entity_type:
            params.append(entity_type)
            sql += f" AND entity_type = ${len(params)}"
        params.extend([limit, offset])
        sql += f" ORDER BY created_at DESC LIMIT ${len(params)-1} OFFSET ${len(params)}"
        async with get_connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


class SyncLogService:

    async def list_sync_logs(self, user: UserContext, limit: int = 50, offset: int = 0) -> list[dict]:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            rows = await conn.fetch(
                "SELECT * FROM sync_logs WHERE user_id = $1 ORDER BY synced_at DESC LIMIT $2 OFFSET $3",
                uid, limit, offset,
            )
        return [dict(r) for r in rows]


class HelpArticleService:

    async def list_articles(self, category: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM help_articles WHERE is_published = true"
        params = []
        if category:
            params.append(category)
            sql += f" AND category = ${len(params)}"
        sql += ' ORDER BY "order", title'
        async with get_connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    async def get_article(self, article_id: int) -> dict:
        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM help_articles WHERE id = $1 AND is_published = true",
                article_id,
            )
        if not row:
            raise NotFoundError("HelpArticle", article_id)
        return dict(row)


class PaymentReminderService:

    async def list_reminders(self, user: UserContext, limit: int = 50, offset: int = 0) -> list[dict]:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            rows = await conn.fetch(
                "SELECT * FROM payment_reminders WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                uid, limit, offset,
            )
        return [dict(r) for r in rows]


class TrialEligibilityService:

    async def get_trial_status(self, user: UserContext) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM trial_eligibility WHERE user_id = $1", uid
            )
        if not row:
            return {"eligible": True, "used": False}
        return dict(row)


class UserFunnelService:

    async def list_events(self, user: UserContext, limit: int = 100, offset: int = 0) -> list[dict]:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            rows = await conn.fetch(
                "SELECT * FROM user_funnel_events WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                uid, limit, offset,
            )
        return [dict(r) for r in rows]


class TableSessionDeviceService:

    async def list_devices(self, user: UserContext, session_id: Optional[str] = None) -> list[dict]:
        clause, params = tenant_where_clause(user)
        sql = f"SELECT * FROM table_session_devices WHERE {clause}"
        if session_id:
            params.append(session_id)
            sql += f" AND session_id = ${len(params)}"
        sql += " ORDER BY joined_at DESC"
        async with get_connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]
