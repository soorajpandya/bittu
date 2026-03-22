"""Favourite Items Service — CRUD for favourite_items."""
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class FavouriteService:

    async def list_favourites(self, user: UserContext) -> list[dict]:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT f.*, i."Item_Name" as item_name, i.price
                FROM favourite_items f
                LEFT JOIN items i ON i."Item_ID" = f.item_id
                WHERE f.user_id = $1
                ORDER BY f.created_at DESC
                """,
                uid,
            )
        return [dict(r) for r in rows]

    async def add_favourite(self, user: UserContext, item_id: int) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO favourite_items (user_id, item_id, restaurant_id)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id, item_id) DO NOTHING
                RETURNING *
                """,
                uid, item_id, user.restaurant_id,
            )
            if not row:
                row = await conn.fetchrow(
                    "SELECT * FROM favourite_items WHERE user_id = $1 AND item_id = $2",
                    uid, item_id,
                )
        return dict(row)

    async def remove_favourite(self, user: UserContext, item_id: int) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                "DELETE FROM favourite_items WHERE user_id = $1 AND item_id = $2 RETURNING id",
                uid, item_id,
            )
        if not row:
            raise NotFoundError("Favourite", str(item_id))
        return {"deleted": True, "item_id": item_id}
