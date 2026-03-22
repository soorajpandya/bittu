"""Deliverable Pincodes Service — CRUD for deliverable_pincodes."""
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.tenant import tenant_where_clause, tenant_insert_fields
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class PincodeService:

    async def list_pincodes(self, user: UserContext) -> list[dict]:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            rows = await conn.fetch(
                "SELECT * FROM deliverable_pincodes WHERE user_id = $1 ORDER BY pincode",
                uid,
            )
        return [dict(r) for r in rows]

    async def create_pincode(self, user: UserContext, data: dict) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO deliverable_pincodes (user_id, pincode, area_name, city, state)
                VALUES ($1,$2,$3,$4,$5)
                RETURNING *
                """,
                uid, data["pincode"], data.get("area_name"),
                data.get("city"), data.get("state"),
            )
        logger.info("pincode_added", pincode=data["pincode"])
        return dict(row)

    async def delete_pincode(self, user: UserContext, pincode_id: int) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                "DELETE FROM deliverable_pincodes WHERE id = $1 AND user_id = $2 RETURNING id",
                pincode_id, uid,
            )
        if not row:
            raise NotFoundError("Pincode", str(pincode_id))
        return {"deleted": True, "id": row["id"]}
