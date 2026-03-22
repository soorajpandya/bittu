"""Cash Transaction Service — CRUD for cash register operations."""
from typing import Optional
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.tenant import tenant_where_clause, tenant_insert_fields
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

VALID_TYPES = {"expense", "withdrawal", "topup", "sale", "refund"}


class CashTransactionService:

    async def list_transactions(self, user: UserContext, tx_type: Optional[str] = None, limit: int = 50, offset: int = 0) -> list[dict]:
        clause, params = tenant_where_clause(user)
        sql = f"SELECT * FROM cash_transactions WHERE {clause}"
        if tx_type:
            params.append(tx_type)
            sql += f" AND type = ${len(params)}"
        params.extend([limit, offset])
        sql += f" ORDER BY created_at DESC LIMIT ${len(params)-1} OFFSET ${len(params)}"
        async with get_connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    async def get_transaction(self, user: UserContext, tx_id: int) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(tx_id)
        async with get_connection() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM cash_transactions WHERE {clause} AND id = ${len(params)}",
                *params,
            )
        if not row:
            raise NotFoundError("CashTransaction", str(tx_id))
        return dict(row)

    async def create_transaction(self, user: UserContext, data: dict) -> dict:
        if data.get("type") and data["type"] not in VALID_TYPES:
            raise ValidationError(f"Invalid type. Must be one of: {', '.join(VALID_TYPES)}")
        tenant = tenant_insert_fields(user)
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO cash_transactions (
                    user_id, branch_id, type, amount, description, category, payment_method
                ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                RETURNING *
                """,
                tenant["user_id"],
                tenant.get("branch_id"),
                data["type"],
                data["amount"],
                data.get("description"),
                data.get("category"),
                data.get("payment_method", "cash"),
            )
        logger.info("cash_tx_created", id=row["id"], type=data["type"], amount=data["amount"])
        return dict(row)

    async def delete_transaction(self, user: UserContext, tx_id: int) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(tx_id)
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                f"DELETE FROM cash_transactions WHERE {clause} AND id = ${len(params)} RETURNING id",
                *params,
            )
        if not row:
            raise NotFoundError("CashTransaction", str(tx_id))
        return {"deleted": True, "id": row["id"]}
