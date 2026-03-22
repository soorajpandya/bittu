"""Due Payment Service — CRUD for due/pending customer payments."""
from typing import Optional
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.tenant import tenant_where_clause, tenant_insert_fields
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

VALID_STATUSES = {"pending", "partial", "paid", "overdue", "written_off"}


class DuePaymentService:

    async def list_due_payments(self, user: UserContext, status: Optional[str] = None, customer_id: Optional[int] = None, limit: int = 50, offset: int = 0) -> list[dict]:
        clause, params = tenant_where_clause(user)
        sql = f"SELECT * FROM due_payments WHERE {clause}"
        if status:
            params.append(status)
            sql += f" AND status = ${len(params)}"
        if customer_id:
            params.append(customer_id)
            sql += f" AND customer_id = ${len(params)}"
        params.extend([limit, offset])
        sql += f" ORDER BY created_at DESC LIMIT ${len(params)-1} OFFSET ${len(params)}"
        async with get_connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    async def get_due_payment(self, user: UserContext, dp_id: int) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(dp_id)
        async with get_connection() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM due_payments WHERE {clause} AND id = ${len(params)}",
                *params,
            )
        if not row:
            raise NotFoundError("DuePayment", str(dp_id))
        return dict(row)

    async def create_due_payment(self, user: UserContext, data: dict) -> dict:
        tenant = tenant_insert_fields(user)
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO due_payments (
                    user_id, branch_id, customer_id, order_id,
                    total_amount, paid_amount, due_amount, status, due_date, notes
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                RETURNING *
                """,
                tenant["user_id"],
                tenant.get("branch_id"),
                data.get("customer_id"),
                data.get("order_id"),
                data["total_amount"],
                data.get("paid_amount", 0),
                data.get("due_amount", data["total_amount"] - data.get("paid_amount", 0)),
                data.get("status", "pending"),
                data.get("due_date"),
                data.get("notes"),
            )
        logger.info("due_payment_created", id=row["id"], amount=data["total_amount"])
        return dict(row)

    async def record_payment(self, user: UserContext, dp_id: int, amount: float) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(dp_id)
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                f"SELECT * FROM due_payments WHERE {clause} AND id = ${len(params)} FOR UPDATE",
                *params,
            )
            if not existing:
                raise NotFoundError("DuePayment", str(dp_id))

            new_paid = float(existing["paid_amount"]) + amount
            new_due = float(existing["total_amount"]) - new_paid
            new_status = "paid" if new_due <= 0 else "partial"

            row = await conn.fetchrow(
                f"""
                UPDATE due_payments SET paid_amount = $1, due_amount = $2, status = $3
                WHERE {clause} AND id = ${len(params)}
                RETURNING *
                """,
                new_paid, max(new_due, 0), new_status, *params,
            )
        return dict(row)

    async def update_status(self, user: UserContext, dp_id: int, status: str) -> dict:
        if status not in VALID_STATUSES:
            raise ValidationError(f"Invalid status. Must be one of: {', '.join(VALID_STATUSES)}")
        clause, params = tenant_where_clause(user)
        params.append(dp_id)
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                f"SELECT * FROM due_payments WHERE {clause} AND id = ${len(params)} FOR UPDATE",
                *params,
            )
            if not existing:
                raise NotFoundError("DuePayment", str(dp_id))
            params.append(status)
            row = await conn.fetchrow(
                f"UPDATE due_payments SET status = ${len(params)} WHERE {clause} AND id = ${len(params)-1} RETURNING *",
                *params,
            )
        return dict(row)
