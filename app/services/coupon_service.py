"""Coupon Service — CRUD for coupons + read coupon_usage."""
from typing import Optional
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.tenant import tenant_where_clause, tenant_insert_fields
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class CouponService:

    async def list_coupons(self, user: UserContext, active_only: bool = False) -> list[dict]:
        clause, params = tenant_where_clause(user)
        sql = f"SELECT * FROM coupons WHERE {clause}"
        if active_only:
            sql += " AND is_active = true"
        sql += " ORDER BY id DESC"
        async with get_connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    async def get_coupon(self, user: UserContext, coupon_id: int) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(coupon_id)
        async with get_connection() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM coupons WHERE {clause} AND id = ${len(params)}",
                *params,
            )
        if not row:
            raise NotFoundError("Coupon", str(coupon_id))
        return dict(row)

    async def create_coupon(self, user: UserContext, data: dict) -> dict:
        tenant = tenant_insert_fields(user)
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO coupons (
                    user_id, branch_id, code, title, type, discount_value,
                    min_order_value, max_discount, usage_limit, user_usage_limit,
                    valid_from, valid_until, is_active, restaurant_id
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                RETURNING *
                """,
                tenant["user_id"],
                tenant.get("branch_id"),
                data["code"].upper(),
                data.get("title"),
                data.get("type", "percentage"),
                data["discount_value"],
                data.get("min_order_value", 0),
                data.get("max_discount"),
                data.get("usage_limit"),
                data.get("user_usage_limit"),
                data.get("valid_from"),
                data.get("valid_until"),
                data.get("is_active", True),
                user.restaurant_id,
            )
        logger.info("coupon_created", id=row["id"], code=data["code"])
        return dict(row)

    async def update_coupon(self, user: UserContext, coupon_id: int, data: dict) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(coupon_id)
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                f"SELECT * FROM coupons WHERE {clause} AND id = ${len(params)} FOR UPDATE",
                *params,
            )
            if not existing:
                raise NotFoundError("Coupon", str(coupon_id))

            fields = {k: v for k, v in data.items() if v is not None}
            if "code" in fields:
                fields["code"] = fields["code"].upper()
            if not fields:
                return dict(existing)

            set_parts = []
            vals = list(params)
            for k, v in fields.items():
                vals.append(v)
                set_parts.append(f"{k} = ${len(vals)}")

            row = await conn.fetchrow(
                f"UPDATE coupons SET {', '.join(set_parts)} WHERE {clause} AND id = ${len(params)} RETURNING *",
                *vals,
            )
        return dict(row)

    async def delete_coupon(self, user: UserContext, coupon_id: int) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(coupon_id)
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                f"DELETE FROM coupons WHERE {clause} AND id = ${len(params)} RETURNING id",
                *params,
            )
        if not row:
            raise NotFoundError("Coupon", str(coupon_id))
        return {"deleted": True, "id": row["id"]}

    async def get_coupon_usage(self, user: UserContext, coupon_id: int) -> list[dict]:
        clause, params = tenant_where_clause(user, "c")
        params.append(coupon_id)
        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""
                SELECT cu.* FROM coupon_usage cu
                JOIN coupons c ON c.id = cu.coupon_id
                WHERE {clause} AND cu.coupon_id = ${len(params)}
                ORDER BY cu.used_at DESC
                """,
                *params,
            )
        return [dict(r) for r in rows]
