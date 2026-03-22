"""Billing & Invoice Service — Read-only for billing_history and invoices."""
from app.core.auth import UserContext
from app.core.database import get_connection
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class BillingService:

    async def list_billing_history(self, user: UserContext, limit: int = 50, offset: int = 0) -> list[dict]:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            rows = await conn.fetch(
                "SELECT * FROM billing_history WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                uid, limit, offset,
            )
        return [dict(r) for r in rows]

    async def get_billing_record(self, user: UserContext, record_id: int) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM billing_history WHERE id = $1 AND user_id = $2",
                record_id, uid,
            )
        if not row:
            raise NotFoundError("BillingRecord", record_id)
        return dict(row)

    async def list_invoices(self, user: UserContext, limit: int = 50, offset: int = 0) -> list[dict]:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            rows = await conn.fetch(
                "SELECT * FROM invoices WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                uid, limit, offset,
            )
        return [dict(r) for r in rows]

    async def get_invoice(self, user: UserContext, invoice_id: int) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM invoices WHERE id = $1 AND user_id = $2",
                invoice_id, uid,
            )
        if not row:
            raise NotFoundError("Invoice", invoice_id)
        return dict(row)
