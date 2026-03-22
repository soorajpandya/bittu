"""Feedback Service — CRUD for customer feedback/reviews."""
from typing import Optional
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.tenant import tenant_where_clause, tenant_insert_fields
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class FeedbackService:

    async def list_feedback(self, user: UserContext, limit: int = 50, offset: int = 0) -> list[dict]:
        clause, params = tenant_where_clause(user)
        params.extend([limit, offset])
        sql = f"SELECT * FROM feedback WHERE {clause} ORDER BY created_at DESC LIMIT ${len(params)-1} OFFSET ${len(params)}"
        async with get_connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    async def get_feedback(self, user: UserContext, feedback_id: int) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(feedback_id)
        async with get_connection() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM feedback WHERE {clause} AND id = ${len(params)}",
                *params,
            )
        if not row:
            raise NotFoundError("Feedback", str(feedback_id))
        return dict(row)

    async def create_feedback(self, user: UserContext, data: dict) -> dict:
        tenant = tenant_insert_fields(user)
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO feedback (
                    user_id, branch_id, customer_id, order_id, rating,
                    food_rating, service_rating, ambience_rating,
                    comment, source
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                RETURNING *
                """,
                tenant["user_id"],
                tenant.get("branch_id"),
                data.get("customer_id"),
                data.get("order_id"),
                data["rating"],
                data.get("food_rating"),
                data.get("service_rating"),
                data.get("ambience_rating"),
                data.get("comment"),
                data.get("source", "pos"),
            )
        logger.info("feedback_created", id=row["id"], rating=data["rating"])
        return dict(row)

    async def respond_to_feedback(self, user: UserContext, feedback_id: int, response: str) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(feedback_id)
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                f"SELECT * FROM feedback WHERE {clause} AND id = ${len(params)} FOR UPDATE",
                *params,
            )
            if not existing:
                raise NotFoundError("Feedback", str(feedback_id))
            params.append(response)
            row = await conn.fetchrow(
                f"UPDATE feedback SET staff_response = ${len(params)}, responded = true WHERE {clause} AND id = ${len(params)-1} RETURNING *",
                *params,
            )
        return dict(row)

    async def delete_feedback(self, user: UserContext, feedback_id: int) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(feedback_id)
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                f"DELETE FROM feedback WHERE {clause} AND id = ${len(params)} RETURNING id",
                *params,
            )
        if not row:
            raise NotFoundError("Feedback", str(feedback_id))
        return {"deleted": True, "id": row["id"]}
