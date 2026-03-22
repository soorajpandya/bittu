"""Combo Service — CRUD for combo meals + combo_items."""
from typing import Optional
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.tenant import tenant_where_clause, tenant_insert_fields
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class ComboService:

    async def list_combos(self, user: UserContext, active_only: bool = False) -> list[dict]:
        clause, params = tenant_where_clause(user)
        sql = f"SELECT * FROM combos WHERE {clause}"
        if active_only:
            sql += " AND is_active = true"
        sql += " ORDER BY name"
        async with get_connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    async def get_combo(self, user: UserContext, combo_id: int) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(combo_id)
        async with get_connection() as conn:
            combo = await conn.fetchrow(
                f"SELECT * FROM combos WHERE {clause} AND id = ${len(params)}",
                *params,
            )
            if not combo:
                raise NotFoundError("Combo", combo_id)
            items = await conn.fetch(
                "SELECT ci.*, i.\"Item_Name\" as item_name FROM combo_items ci LEFT JOIN items i ON i.\"Item_ID\" = ci.item_id WHERE ci.combo_id = $1",
                combo_id,
            )
        result = dict(combo)
        result["items"] = [dict(i) for i in items]
        return result

    async def create_combo(self, user: UserContext, data: dict) -> dict:
        tenant = tenant_insert_fields(user)
        items = data.pop("items", [])
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO combos (user_id, branch_id, restaurant_id, name, description, price, image_url, is_active)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                RETURNING *
                """,
                tenant["user_id"],
                tenant.get("branch_id"),
                user.restaurant_id,
                data["name"],
                data.get("description"),
                data["price"],
                data.get("image_url"),
                data.get("is_active", True),
            )
            combo_id = row["id"]
            for item in items:
                await conn.execute(
                    "INSERT INTO combo_items (combo_id, item_id, quantity) VALUES ($1,$2,$3)",
                    combo_id, item["item_id"], item.get("quantity", 1),
                )
        result = dict(row)
        result["items"] = items
        logger.info("combo_created", id=str(combo_id), name=data["name"])
        return result

    async def update_combo(self, user: UserContext, combo_id: int, data: dict) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(combo_id)
        items = data.pop("items", None)
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                f"SELECT * FROM combos WHERE {clause} AND id = ${len(params)} FOR UPDATE",
                *params,
            )
            if not existing:
                raise NotFoundError("Combo", combo_id)

            fields = {k: v for k, v in data.items() if v is not None}
            if fields:
                set_parts = []
                vals = list(params)
                for k, v in fields.items():
                    vals.append(v)
                    set_parts.append(f"{k} = ${len(vals)}")
                await conn.execute(
                    f"UPDATE combos SET {', '.join(set_parts)} WHERE {clause} AND id = ${len(params)}",
                    *vals,
                )

            if items is not None:
                await conn.execute("DELETE FROM combo_items WHERE combo_id = $1", combo_id)
                for item in items:
                    await conn.execute(
                        "INSERT INTO combo_items (combo_id, item_id, quantity) VALUES ($1,$2,$3)",
                        combo_id, item["item_id"], item.get("quantity", 1),
                    )

            row = await conn.fetchrow(
                f"SELECT * FROM combos WHERE id = $1", combo_id
            )
        return dict(row)

    async def delete_combo(self, user: UserContext, combo_id: int) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(combo_id)
        async with get_serializable_transaction() as conn:
            await conn.execute("DELETE FROM combo_items WHERE combo_id = $1", combo_id)
            row = await conn.fetchrow(
                f"DELETE FROM combos WHERE {clause} AND id = ${len(params)} RETURNING id",
                *params,
            )
        if not row:
            raise NotFoundError("Combo", combo_id)
        return {"deleted": True, "id": str(row["id"])}
