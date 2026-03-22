"""Item Ingredient Service — CRUD for item_ingredients (recipe linkages)."""
from typing import Optional
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class ItemIngredientService:

    async def list_ingredients(self, user: UserContext, item_id: Optional[int] = None) -> list[dict]:
        uid = user.owner_id if user.is_branch_user else user.user_id
        if item_id:
            sql = """
                SELECT ii.*, ing.name as ingredient_name
                FROM item_ingredients ii
                LEFT JOIN ingredients ing ON ing.id = ii.ingredient_id
                WHERE ii.user_id = $1 AND ii.item_id = $2
                ORDER BY ing.name
            """
            async with get_connection() as conn:
                rows = await conn.fetch(sql, uid, item_id)
        else:
            sql = """
                SELECT ii.*, ing.name as ingredient_name
                FROM item_ingredients ii
                LEFT JOIN ingredients ing ON ing.id = ii.ingredient_id
                WHERE ii.user_id = $1
                ORDER BY ii.item_id, ing.name
            """
            async with get_connection() as conn:
                rows = await conn.fetch(sql, uid)
        return [dict(r) for r in rows]

    async def add_ingredient(self, user: UserContext, data: dict) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO item_ingredients (user_id, item_id, ingredient_id, quantity_used, unit)
                VALUES ($1,$2,$3,$4,$5)
                RETURNING *
                """,
                uid, data["item_id"], data["ingredient_id"],
                data.get("quantity_used", 0), data.get("unit"),
            )
        logger.info("item_ingredient_added", id=str(row["id"]))
        return dict(row)

    async def update_ingredient(self, user: UserContext, ii_id: int, data: dict) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                "SELECT * FROM item_ingredients WHERE id = $1 AND user_id = $2 FOR UPDATE",
                ii_id, uid,
            )
            if not existing:
                raise NotFoundError("ItemIngredient", ii_id)
            fields = {k: v for k, v in data.items() if v is not None}
            if not fields:
                return dict(existing)
            set_parts = []
            vals = [ii_id, uid]
            for k, v in fields.items():
                vals.append(v)
                set_parts.append(f"{k} = ${len(vals)}")
            row = await conn.fetchrow(
                f"UPDATE item_ingredients SET {', '.join(set_parts)} WHERE id = $1 AND user_id = $2 RETURNING *",
                *vals,
            )
        return dict(row)

    async def remove_ingredient(self, user: UserContext, ii_id: int) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                "DELETE FROM item_ingredients WHERE id = $1 AND user_id = $2 RETURNING id",
                ii_id, uid,
            )
        if not row:
            raise NotFoundError("ItemIngredient", ii_id)
        return {"deleted": True, "id": str(row["id"])}
