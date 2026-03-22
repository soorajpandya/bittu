"""Purchase Order Service — CRUD for purchase_orders + purchase_order_items."""
from typing import Optional
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.tenant import tenant_where_clause, tenant_insert_fields
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

VALID_STATUSES = {"draft", "ordered", "received", "partial", "cancelled"}


class PurchaseOrderService:

    async def list_orders(self, user: UserContext, status: Optional[str] = None, limit: int = 50, offset: int = 0) -> list[dict]:
        clause, params = tenant_where_clause(user)
        sql = f"SELECT * FROM purchase_orders WHERE {clause}"
        if status:
            params.append(status)
            sql += f" AND status = ${len(params)}"
        params.extend([limit, offset])
        sql += f" ORDER BY created_at DESC LIMIT ${len(params)-1} OFFSET ${len(params)}"
        async with get_connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    async def get_order(self, user: UserContext, po_id: int) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(po_id)
        async with get_connection() as conn:
            po = await conn.fetchrow(
                f"SELECT * FROM purchase_orders WHERE {clause} AND id = ${len(params)}",
                *params,
            )
            if not po:
                raise NotFoundError("PurchaseOrder", po_id)
            items = await conn.fetch(
                "SELECT * FROM purchase_order_items WHERE purchase_order_id = $1 ORDER BY id",
                po_id,
            )
        result = dict(po)
        result["items"] = [dict(i) for i in items]
        return result

    async def create_order(self, user: UserContext, data: dict) -> dict:
        tenant = tenant_insert_fields(user)
        items = data.pop("items", [])
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO purchase_orders (
                    user_id, branch_id, supplier_name, supplier_contact,
                    status, notes, expected_delivery_date, total_amount
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                RETURNING *
                """,
                tenant["user_id"],
                tenant.get("branch_id"),
                data.get("supplier_name"),
                data.get("supplier_contact"),
                data.get("status", "draft"),
                data.get("notes"),
                data.get("expected_delivery_date"),
                data.get("total_amount", 0),
            )
            po_id = row["id"]
            created_items = []
            for item in items:
                i = await conn.fetchrow(
                    """
                    INSERT INTO purchase_order_items (
                        purchase_order_id, ingredient_id, ingredient_name,
                        quantity_ordered, unit, unit_price
                    ) VALUES ($1,$2,$3,$4,$5,$6)
                    RETURNING *
                    """,
                    po_id, item.get("ingredient_id"), item.get("ingredient_name"),
                    item.get("quantity_ordered", 0), item.get("unit"),
                    item.get("unit_price", 0),
                )
                created_items.append(dict(i))
        result = dict(row)
        result["items"] = created_items
        logger.info("purchase_order_created", id=str(po_id))
        return result

    async def update_order(self, user: UserContext, po_id: int, data: dict) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(po_id)
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                f"SELECT * FROM purchase_orders WHERE {clause} AND id = ${len(params)} FOR UPDATE",
                *params,
            )
            if not existing:
                raise NotFoundError("PurchaseOrder", po_id)

            fields = {k: v for k, v in data.items() if v is not None and k != "items"}
            if "status" in fields and fields["status"] not in VALID_STATUSES:
                raise ValidationError(f"Invalid status. Must be one of: {', '.join(VALID_STATUSES)}")

            if fields:
                set_parts = []
                vals = list(params)
                for k, v in fields.items():
                    vals.append(v)
                    set_parts.append(f"{k} = ${len(vals)}")
                row = await conn.fetchrow(
                    f"UPDATE purchase_orders SET {', '.join(set_parts)} WHERE {clause} AND id = ${len(params)} RETURNING *",
                    *vals,
                )
            else:
                row = existing
        return dict(row)

    async def delete_order(self, user: UserContext, po_id: int) -> dict:
        clause, params = tenant_where_clause(user)
        params.append(po_id)
        async with get_serializable_transaction() as conn:
            await conn.execute("DELETE FROM purchase_order_items WHERE purchase_order_id = $1", po_id)
            row = await conn.fetchrow(
                f"DELETE FROM purchase_orders WHERE {clause} AND id = ${len(params)} RETURNING id",
                *params,
            )
        if not row:
            raise NotFoundError("PurchaseOrder", po_id)
        return {"deleted": True, "id": str(row["id"])}
