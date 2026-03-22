"""Item Variant Service — CRUD for item variants."""
from typing import Optional
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class ItemVariantService:

    async def list_variants(self, user: UserContext, item_id: Optional[int] = None) -> list[dict]:
        uid = user.owner_id if user.is_branch_user else user.user_id
        if item_id:
            sql = "SELECT * FROM item_variants WHERE user_id = $1 AND item_id = $2 ORDER BY name"
            async with get_connection() as conn:
                rows = await conn.fetch(sql, uid, item_id)
        else:
            sql = "SELECT * FROM item_variants WHERE user_id = $1 ORDER BY item_id, name"
            async with get_connection() as conn:
                rows = await conn.fetch(sql, uid)
        return [dict(r) for r in rows]

    async def create_variant(self, user: UserContext, data: dict) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO item_variants (user_id, item_id, name, price, is_active, sku)
                VALUES ($1,$2,$3,$4,$5,$6)
                RETURNING *
                """,
                uid, data["item_id"], data["name"], data["price"],
                data.get("is_active", True), data.get("sku"),
            )
        logger.info("variant_created", id=str(row["id"]))
        return dict(row)

    async def update_variant(self, user: UserContext, variant_id: int, data: dict) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                "SELECT * FROM item_variants WHERE id = $1 AND user_id = $2 FOR UPDATE",
                variant_id, uid,
            )
            if not existing:
                raise NotFoundError("ItemVariant", variant_id)
            fields = {k: v for k, v in data.items() if v is not None}
            if not fields:
                return dict(existing)
            set_parts = []
            vals = [variant_id, uid]
            for k, v in fields.items():
                vals.append(v)
                set_parts.append(f"{k} = ${len(vals)}")
            row = await conn.fetchrow(
                f"UPDATE item_variants SET {', '.join(set_parts)} WHERE id = $1 AND user_id = $2 RETURNING *",
                *vals,
            )
        return dict(row)

    async def delete_variant(self, user: UserContext, variant_id: int) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                "DELETE FROM item_variants WHERE id = $1 AND user_id = $2 RETURNING id",
                variant_id, uid,
            )
        if not row:
            raise NotFoundError("ItemVariant", variant_id)
        return {"deleted": True, "id": str(row["id"])}


class ItemAddonService:

    async def list_addons(self, user: UserContext, item_id: Optional[int] = None) -> list[dict]:
        uid = user.owner_id if user.is_branch_user else user.user_id
        if item_id:
            sql = "SELECT * FROM item_addons WHERE user_id = $1 AND item_id = $2 ORDER BY name"
            async with get_connection() as conn:
                rows = await conn.fetch(sql, uid, item_id)
        else:
            sql = "SELECT * FROM item_addons WHERE user_id = $1 ORDER BY item_id, name"
            async with get_connection() as conn:
                rows = await conn.fetch(sql, uid)
        return [dict(r) for r in rows]

    async def create_addon(self, user: UserContext, data: dict) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO item_addons (user_id, item_id, name, price, is_active)
                VALUES ($1,$2,$3,$4,$5)
                RETURNING *
                """,
                uid, data["item_id"], data["name"], data["price"],
                data.get("is_active", True),
            )
        logger.info("addon_created", id=str(row["id"]))
        return dict(row)

    async def update_addon(self, user: UserContext, addon_id: int, data: dict) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                "SELECT * FROM item_addons WHERE id = $1 AND user_id = $2 FOR UPDATE",
                addon_id, uid,
            )
            if not existing:
                raise NotFoundError("ItemAddon", addon_id)
            fields = {k: v for k, v in data.items() if v is not None}
            if not fields:
                return dict(existing)
            set_parts = []
            vals = [addon_id, uid]
            for k, v in fields.items():
                vals.append(v)
                set_parts.append(f"{k} = ${len(vals)}")
            row = await conn.fetchrow(
                f"UPDATE item_addons SET {', '.join(set_parts)} WHERE id = $1 AND user_id = $2 RETURNING *",
                *vals,
            )
        return dict(row)

    async def delete_addon(self, user: UserContext, addon_id: int) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                "DELETE FROM item_addons WHERE id = $1 AND user_id = $2 RETURNING id",
                addon_id, uid,
            )
        if not row:
            raise NotFoundError("ItemAddon", addon_id)
        return {"deleted": True, "id": str(row["id"])}


class ItemExtraService:

    async def list_extras(self, user: UserContext, item_id: Optional[int] = None) -> list[dict]:
        uid = user.owner_id if user.is_branch_user else user.user_id
        if item_id:
            sql = "SELECT * FROM item_extras WHERE user_id = $1 AND item_id = $2 ORDER BY name"
            async with get_connection() as conn:
                rows = await conn.fetch(sql, uid, item_id)
        else:
            sql = "SELECT * FROM item_extras WHERE user_id = $1 ORDER BY item_id, name"
            async with get_connection() as conn:
                rows = await conn.fetch(sql, uid)
        return [dict(r) for r in rows]

    async def create_extra(self, user: UserContext, data: dict) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO item_extras (user_id, item_id, name, price, is_active)
                VALUES ($1,$2,$3,$4,$5)
                RETURNING *
                """,
                uid, data["item_id"], data["name"], data["price"],
                data.get("is_active", True),
            )
        logger.info("extra_created", id=str(row["id"]))
        return dict(row)

    async def update_extra(self, user: UserContext, extra_id: int, data: dict) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                "SELECT * FROM item_extras WHERE id = $1 AND user_id = $2 FOR UPDATE",
                extra_id, uid,
            )
            if not existing:
                raise NotFoundError("ItemExtra", extra_id)
            fields = {k: v for k, v in data.items() if v is not None}
            if not fields:
                return dict(existing)
            set_parts = []
            vals = [extra_id, uid]
            for k, v in fields.items():
                vals.append(v)
                set_parts.append(f"{k} = ${len(vals)}")
            row = await conn.fetchrow(
                f"UPDATE item_extras SET {', '.join(set_parts)} WHERE id = $1 AND user_id = $2 RETURNING *",
                *vals,
            )
        return dict(row)

    async def delete_extra(self, user: UserContext, extra_id: int) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                "DELETE FROM item_extras WHERE id = $1 AND user_id = $2 RETURNING id",
                extra_id, uid,
            )
        if not row:
            raise NotFoundError("ItemExtra", extra_id)
        return {"deleted": True, "id": str(row["id"])}
