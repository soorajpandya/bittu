"""Category Service — CRUD for menu categories."""
from typing import Optional
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.tenant import tenant_where_clause, tenant_insert_fields
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)


class CategoryService:

    async def list_categories(self, user: UserContext, active_only: bool = False) -> list[dict]:
        clause, params = tenant_where_clause(user)
        sql = f"SELECT * FROM categories WHERE {clause}"
        if active_only:
            sql += " AND is_active = true"
        sql += " ORDER BY sort_order, name"
        async with get_connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    async def get_category(self, user: UserContext, category_id: int) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(category_id)
        async with get_connection() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM categories WHERE {clause} AND id = ${len(params)}",
                *params,
            )
        if not row:
            raise NotFoundError("Category", category_id)
        return dict(row)

    async def create_category(self, user: UserContext, data: dict) -> dict:
        tenant = tenant_insert_fields(user)
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO categories (user_id, branch_id, name, slug, description, image_url, sort_order, is_active)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING *
                """,
                tenant["user_id"],
                tenant.get("branch_id"),
                data["name"],
                data.get("slug", data["name"].lower().replace(" ", "-")),
                data.get("description"),
                data.get("image_url"),
                data.get("sort_order", 0),
                data.get("is_active", True),
            )
        logger.info("category_created", id=str(row["id"]), name=data["name"])
        return dict(row)

    async def update_category(self, user: UserContext, category_id: int, data: dict) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(category_id)
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                f"SELECT * FROM categories WHERE {clause} AND id = ${len(params)} FOR UPDATE",
                *params,
            )
            if not existing:
                raise NotFoundError("Category", category_id)

            fields = {k: v for k, v in data.items() if v is not None}
            if not fields:
                return dict(existing)

            set_parts = []
            vals = list(params)
            for k, v in fields.items():
                vals.append(v)
                set_parts.append(f"{k} = ${len(vals)}")

            row = await conn.fetchrow(
                f"UPDATE categories SET {', '.join(set_parts)} WHERE {clause} AND id = ${len(params)} RETURNING *",
                *vals,
            )
        return dict(row)

    async def delete_category(self, user: UserContext, category_id: int) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(category_id)
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                f"DELETE FROM categories WHERE {clause} AND id = ${len(params)} RETURNING id",
                *params,
            )
        if not row:
            raise NotFoundError("Category", category_id)
        return {"deleted": True, "id": str(row["id"])}
