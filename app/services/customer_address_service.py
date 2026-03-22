"""Customer Address Service — CRUD for customer_addresses."""
from typing import Optional
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.tenant import tenant_where_clause
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class CustomerAddressService:

    async def list_addresses(self, user: UserContext, customer_id: int) -> list[dict]:
        clause, params = tenant_where_clause(user, "c")
        params.append(customer_id)
        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""
                SELECT ca.* FROM customer_addresses ca
                JOIN customers c ON c.id = ca.customer_id
                WHERE {clause} AND ca.customer_id = ${len(params)}
                ORDER BY ca.is_default DESC, ca.label
                """,
                *params,
            )
        return [dict(r) for r in rows]

    async def create_address(self, user: UserContext, customer_id: int, data: dict) -> dict:
        # Verify customer belongs to tenant
        clause, params = tenant_where_clause(user)
        params.append(customer_id)
        async with get_serializable_transaction() as conn:
            cust = await conn.fetchrow(
                f"SELECT id FROM customers WHERE {clause} AND id = ${len(params)}",
                *params,
            )
            if not cust:
                raise NotFoundError("Customer", str(customer_id))
            row = await conn.fetchrow(
                """
                INSERT INTO customer_addresses (
                    customer_id, label, address_line, city, state, pincode, lat, lng, is_default
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                RETURNING *
                """,
                customer_id,
                data.get("label", "Home"),
                data.get("address_line"),
                data.get("city"),
                data.get("state"),
                data.get("pincode"),
                data.get("lat"),
                data.get("lng"),
                data.get("is_default", False),
            )
        logger.info("address_created", id=row["id"], customer_id=customer_id)
        return dict(row)

    async def update_address(self, user: UserContext, address_id: int, data: dict) -> dict:
        clause, params = tenant_where_clause(user, "c")
        params.append(address_id)
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                f"""
                SELECT ca.* FROM customer_addresses ca
                JOIN customers c ON c.id = ca.customer_id
                WHERE {clause} AND ca.id = ${len(params)}
                FOR UPDATE OF ca
                """,
                *params,
            )
            if not existing:
                raise NotFoundError("CustomerAddress", str(address_id))
            fields = {k: v for k, v in data.items() if v is not None}
            if not fields:
                return dict(existing)
            set_parts = []
            vals = [address_id]
            for k, v in fields.items():
                vals.append(v)
                set_parts.append(f"{k} = ${len(vals)}")
            row = await conn.fetchrow(
                f"UPDATE customer_addresses SET {', '.join(set_parts)} WHERE id = $1 RETURNING *",
                *vals,
            )
        return dict(row)

    async def delete_address(self, user: UserContext, address_id: int) -> dict:
        clause, params = tenant_where_clause(user, "c")
        params.append(address_id)
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                f"""
                DELETE FROM customer_addresses ca
                USING customers c
                WHERE c.id = ca.customer_id AND {clause} AND ca.id = ${len(params)}
                RETURNING ca.id
                """,
                *params,
            )
        if not row:
            raise NotFoundError("CustomerAddress", str(address_id))
        return {"deleted": True, "id": row["id"]}
