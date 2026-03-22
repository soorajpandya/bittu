"""Offer Service — CRUD for promotional offers."""
from typing import Optional
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.tenant import tenant_where_clause, tenant_insert_fields
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class OfferService:

    async def list_offers(self, user: UserContext, active_only: bool = False) -> list[dict]:
        clause, params = tenant_where_clause(user)
        sql = f"SELECT * FROM offers WHERE {clause}"
        if active_only:
            sql += " AND is_active = true"
        sql += " ORDER BY id DESC"
        async with get_connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    async def get_offer(self, user: UserContext, offer_id: int) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(offer_id)
        async with get_connection() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM offers WHERE {clause} AND id = ${len(params)}",
                *params,
            )
        if not row:
            raise NotFoundError("Offer", str(offer_id))
        return dict(row)

    async def create_offer(self, user: UserContext, data: dict) -> dict:
        tenant = tenant_insert_fields(user)
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO offers (
                    user_id, branch_id, restaurant_id, title, description, discount,
                    code, type, icon, expiry_days, is_active, valid_from, valid_until
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                RETURNING *
                """,
                tenant["user_id"],
                tenant.get("branch_id"),
                user.restaurant_id,
                data["title"],
                data.get("description"),
                data.get("discount", 0),
                data.get("code"),
                data.get("type", "percentage"),
                data.get("icon"),
                data.get("expiry_days"),
                data.get("is_active", True),
                data.get("valid_from"),
                data.get("valid_until"),
            )
        logger.info("offer_created", id=row["id"], title=data["title"])
        return dict(row)

    async def update_offer(self, user: UserContext, offer_id: int, data: dict) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(offer_id)
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                f"SELECT * FROM offers WHERE {clause} AND id = ${len(params)} FOR UPDATE",
                *params,
            )
            if not existing:
                raise NotFoundError("Offer", str(offer_id))

            fields = {k: v for k, v in data.items() if v is not None}
            if not fields:
                return dict(existing)

            set_parts = []
            vals = list(params)
            for k, v in fields.items():
                vals.append(v)
                set_parts.append(f"{k} = ${len(vals)}")

            row = await conn.fetchrow(
                f"UPDATE offers SET {', '.join(set_parts)} WHERE {clause} AND id = ${len(params)} RETURNING *",
                *vals,
            )
        return dict(row)

    async def delete_offer(self, user: UserContext, offer_id: int) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(offer_id)
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                f"DELETE FROM offers WHERE {clause} AND id = ${len(params)} RETURNING id",
                *params,
            )
        if not row:
            raise NotFoundError("Offer", str(offer_id))
        return {"deleted": True, "id": row["id"]}
