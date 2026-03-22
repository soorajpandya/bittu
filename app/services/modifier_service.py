"""Modifier Service — CRUD for modifier_groups + modifier_options."""
from typing import Optional
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class ModifierService:

    # ── Groups ──

    async def list_groups(self, user: UserContext) -> list[dict]:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            rows = await conn.fetch(
                "SELECT * FROM modifier_groups WHERE user_id = $1 ORDER BY name", uid
            )
        return [dict(r) for r in rows]

    async def get_group(self, user: UserContext, group_id: int) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            group = await conn.fetchrow(
                "SELECT * FROM modifier_groups WHERE id = $1 AND user_id = $2", group_id, uid
            )
            if not group:
                raise NotFoundError("ModifierGroup", group_id)
            options = await conn.fetch(
                "SELECT * FROM modifier_options WHERE group_id = $1 ORDER BY name", group_id
            )
        result = dict(group)
        result["options"] = [dict(o) for o in options]
        return result

    async def create_group(self, user: UserContext, data: dict) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        options = data.pop("options", [])
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO modifier_groups (user_id, name, is_required, min_selections, max_selections)
                VALUES ($1,$2,$3,$4,$5)
                RETURNING *
                """,
                uid, data["name"], data.get("is_required", False),
                data.get("min_selections", 0), data.get("max_selections"),
            )
            group_id = row["id"]
            created_options = []
            for opt in options:
                o = await conn.fetchrow(
                    """
                    INSERT INTO modifier_options (group_id, name, price, is_active)
                    VALUES ($1,$2,$3,$4)
                    RETURNING *
                    """,
                    group_id, opt["name"], opt.get("price", 0), opt.get("is_active", True),
                )
                created_options.append(dict(o))
        result = dict(row)
        result["options"] = created_options
        logger.info("modifier_group_created", id=str(group_id))
        return result

    async def update_group(self, user: UserContext, group_id: int, data: dict) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                "SELECT * FROM modifier_groups WHERE id = $1 AND user_id = $2 FOR UPDATE",
                group_id, uid,
            )
            if not existing:
                raise NotFoundError("ModifierGroup", group_id)
            fields = {k: v for k, v in data.items() if v is not None and k != "options"}
            if fields:
                set_parts = []
                vals = [group_id, uid]
                for k, v in fields.items():
                    vals.append(v)
                    set_parts.append(f"{k} = ${len(vals)}")
                await conn.execute(
                    f"UPDATE modifier_groups SET {', '.join(set_parts)} WHERE id = $1 AND user_id = $2",
                    *vals,
                )
            row = await conn.fetchrow(
                "SELECT * FROM modifier_groups WHERE id = $1", group_id
            )
        return dict(row)

    async def delete_group(self, user: UserContext, group_id: int) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            await conn.execute("DELETE FROM modifier_options WHERE group_id = $1", group_id)
            row = await conn.fetchrow(
                "DELETE FROM modifier_groups WHERE id = $1 AND user_id = $2 RETURNING id",
                group_id, uid,
            )
        if not row:
            raise NotFoundError("ModifierGroup", group_id)
        return {"deleted": True, "id": str(row["id"])}

    # ── Options ──

    async def add_option(self, user: UserContext, group_id: int, data: dict) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            group = await conn.fetchrow(
                "SELECT id FROM modifier_groups WHERE id = $1 AND user_id = $2",
                group_id, uid,
            )
            if not group:
                raise NotFoundError("ModifierGroup", group_id)
            row = await conn.fetchrow(
                """
                INSERT INTO modifier_options (group_id, name, price, is_active)
                VALUES ($1,$2,$3,$4)
                RETURNING *
                """,
                group_id, data["name"], data.get("price", 0), data.get("is_active", True),
            )
        return dict(row)

    async def update_option(self, user: UserContext, option_id: int, data: dict) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                """
                SELECT mo.* FROM modifier_options mo
                JOIN modifier_groups mg ON mg.id = mo.group_id
                WHERE mo.id = $1 AND mg.user_id = $2
                FOR UPDATE OF mo
                """,
                option_id, uid,
            )
            if not existing:
                raise NotFoundError("ModifierOption", option_id)
            fields = {k: v for k, v in data.items() if v is not None}
            if not fields:
                return dict(existing)
            set_parts = []
            vals = [option_id]
            for k, v in fields.items():
                vals.append(v)
                set_parts.append(f"{k} = ${len(vals)}")
            row = await conn.fetchrow(
                f"UPDATE modifier_options SET {', '.join(set_parts)} WHERE id = $1 RETURNING *",
                *vals,
            )
        return dict(row)

    async def delete_option(self, user: UserContext, option_id: int) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                """
                SELECT mo.id FROM modifier_options mo
                JOIN modifier_groups mg ON mg.id = mo.group_id
                WHERE mo.id = $1 AND mg.user_id = $2
                """,
                option_id, uid,
            )
            if not existing:
                raise NotFoundError("ModifierOption", option_id)
            row = await conn.fetchrow(
                "DELETE FROM modifier_options WHERE id = $1 RETURNING id", option_id,
            )
        return {"deleted": True, "id": str(row["id"])}
