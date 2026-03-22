"""Item Station Mapping Service — CRUD for item_station_mapping."""
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class ItemStationService:

    async def list_mappings(self, user: UserContext, item_id: int = None, station_id: int = None) -> list[dict]:
        uid = user.owner_id if user.is_branch_user else user.user_id
        sql = "SELECT * FROM item_station_mapping WHERE user_id = $1"
        params = [uid]
        if item_id:
            params.append(item_id)
            sql += f" AND item_id = ${len(params)}"
        if station_id:
            params.append(station_id)
            sql += f" AND station_id = ${len(params)}"
        sql += " ORDER BY item_id"
        async with get_connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    async def create_mapping(self, user: UserContext, item_id: int, station_id: int) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO item_station_mapping (user_id, item_id, station_id)
                VALUES ($1,$2,$3)
                ON CONFLICT (item_id, station_id) DO NOTHING
                RETURNING *
                """,
                uid, item_id, station_id,
            )
            if not row:
                row = await conn.fetchrow(
                    "SELECT * FROM item_station_mapping WHERE item_id = $1 AND station_id = $2",
                    item_id, station_id,
                )
        return dict(row)

    async def delete_mapping(self, user: UserContext, item_id: int, station_id: int) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                "DELETE FROM item_station_mapping WHERE item_id = $1 AND station_id = $2 AND user_id = $3 RETURNING item_id, station_id",
                item_id, station_id, uid,
            )
        if not row:
            raise NotFoundError("ItemStationMapping", f"{item_id}/{station_id}")
        return {"deleted": True, "item_id": row["item_id"], "station_id": str(row["station_id"])}
