"""Customer Service — CRUD for customer records."""
from typing import Optional
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.tenant import tenant_where_clause, tenant_insert_fields
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class CustomerService:

    async def list_customers(self, user: UserContext, search: Optional[str] = None, limit: int = 50, offset: int = 0) -> list[dict]:
        clause, params = tenant_where_clause(user)
        sql = f"SELECT * FROM customers WHERE {clause}"
        if search:
            params.append(f"%{search}%")
            sql += f" AND (name ILIKE ${len(params)} OR phone_number ILIKE ${len(params)} OR email ILIKE ${len(params)})"
        sql += f" ORDER BY id DESC LIMIT ${len(params)+1} OFFSET ${len(params)+2}"
        params.extend([limit, offset])
        async with get_connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    async def get_customer(self, user: UserContext, customer_id: int) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(customer_id)
        async with get_connection() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM customers WHERE {clause} AND id = ${len(params)}",
                *params,
            )
        if not row:
            raise NotFoundError("Customer", str(customer_id))
        return dict(row)

    async def create_customer(self, user: UserContext, data: dict) -> dict:
        tenant = tenant_insert_fields(user)
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO customers (user_id, branch_id, restaurant_id, name, email, phone_number, address, notes)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING *
                """,
                tenant["user_id"],
                tenant.get("branch_id"),
                user.restaurant_id,
                data["name"],
                data.get("email"),
                data.get("phone_number"),
                data.get("address"),
                data.get("notes"),
            )
        logger.info("customer_created", id=row["id"], name=data["name"])
        return dict(row)

    async def update_customer(self, user: UserContext, customer_id: int, data: dict) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(customer_id)
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                f"SELECT * FROM customers WHERE {clause} AND id = ${len(params)} FOR UPDATE",
                *params,
            )
            if not existing:
                raise NotFoundError("Customer", str(customer_id))

            fields = {k: v for k, v in data.items() if v is not None}
            if not fields:
                return dict(existing)

            set_parts = []
            vals = list(params)
            for k, v in fields.items():
                vals.append(v)
                set_parts.append(f"{k} = ${len(vals)}")

            row = await conn.fetchrow(
                f"UPDATE customers SET {', '.join(set_parts)} WHERE {clause} AND id = ${len(params)} RETURNING *",
                *vals,
            )
        return dict(row)

    async def delete_customer(self, user: UserContext, customer_id: int) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(customer_id)
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                f"DELETE FROM customers WHERE {clause} AND id = ${len(params)} RETURNING id",
                *params,
            )
        if not row:
            raise NotFoundError("Customer", str(customer_id))
        return {"deleted": True, "id": row["id"]}
