"""Purchase Order Service — CRUD for purchase_orders + purchase_order_items."""
from datetime import date, time
from typing import Optional
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.tenant import tenant_where_clause, tenant_insert_fields
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

VALID_STATUSES = {"draft", "ordered", "received", "partial", "cancelled"}
VALID_SOURCE_TYPES = {"supplier", "restaurant", "kitchen"}
VALID_PAYMENT_STATUSES = {"unpaid", "paid"}


class PurchaseOrderService:

    async def _generate_po_number(self, conn, user_id: str) -> str:
        """Auto-generate a unique PO number like PO-1001, PO-1002, etc."""
        seq = await conn.fetchval("SELECT nextval('po_number_seq')")
        return f"PO-{seq}"

    async def list_orders(self, user: UserContext, status: Optional[str] = None,
                          payment_status: Optional[str] = None,
                          source_type: Optional[str] = None,
                          limit: int = 50, offset: int = 0) -> list[dict]:
        clause, params = tenant_where_clause(user)
        sql = f"SELECT * FROM purchase_orders WHERE {clause}"
        if status:
            params.append(status)
            sql += f" AND status = ${len(params)}"
        if payment_status:
            params.append(payment_status)
            sql += f" AND payment_status = ${len(params)}"
        if source_type:
            params.append(source_type)
            sql += f" AND source_type = ${len(params)}"
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

        source_type = data.get("source_type", "supplier")
        if source_type not in VALID_SOURCE_TYPES:
            raise ValidationError(f"Invalid source_type. Must be one of: {', '.join(VALID_SOURCE_TYPES)}")

        payment_status = data.get("payment_status", "unpaid")
        if payment_status not in VALID_PAYMENT_STATUSES:
            raise ValidationError(f"Invalid payment_status. Must be one of: {', '.join(VALID_PAYMENT_STATUSES)}")

        # Calculate sub_total from items
        sub_total = sum(
            (item.get("quantity_ordered", 0) or 0) * (item.get("unit_price", 0) or 0)
            for item in items
        )
        delivery_charges = data.get("delivery_charges", 0) or 0
        total_amount = sub_total + delivery_charges

        async with get_serializable_transaction() as conn:
            po_number = await self._generate_po_number(conn, tenant["user_id"])

            row = await conn.fetchrow(
                """
                INSERT INTO purchase_orders (
                    user_id, branch_id, po_number, source_type, source_id, source_name,
                    supplier_name, supplier_contact,
                    status, notes, expected_delivery_date, delivery_time,
                    sub_total, delivery_charges, total_amount, payment_status
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                RETURNING *
                """,
                tenant["user_id"],
                tenant.get("branch_id"),
                po_number,
                source_type,
                data.get("source_id"),
                data.get("source_name"),
                data.get("supplier_name"),
                data.get("supplier_contact"),
                data.get("status", "draft"),
                data.get("notes"),
                data.get("expected_delivery_date"),
                data.get("delivery_time"),
                sub_total,
                delivery_charges,
                total_amount,
                payment_status,
            )
            po_id = row["id"]
            created_items = []
            for item in items:
                qty = item.get("quantity_ordered", 0) or 0
                price = item.get("unit_price", 0) or 0
                amount = qty * price
                i = await conn.fetchrow(
                    """
                    INSERT INTO purchase_order_items (
                        purchase_order_id, ingredient_id, ingredient_name,
                        quantity_ordered, unit, unit_price, amount
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                    RETURNING *
                    """,
                    po_id, item.get("ingredient_id"), item.get("ingredient_name"),
                    qty, item.get("unit"), price, amount,
                )
                created_items.append(dict(i))
        result = dict(row)
        result["items"] = created_items
        logger.info("purchase_order_created", id=str(po_id), po_number=po_number)
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
            if "source_type" in fields and fields["source_type"] not in VALID_SOURCE_TYPES:
                raise ValidationError(f"Invalid source_type. Must be one of: {', '.join(VALID_SOURCE_TYPES)}")
            if "payment_status" in fields and fields["payment_status"] not in VALID_PAYMENT_STATUSES:
                raise ValidationError(f"Invalid payment_status. Must be one of: {', '.join(VALID_PAYMENT_STATUSES)}")

            # If items are provided, recalculate totals
            new_items = data.get("items")
            if new_items is not None:
                # Replace all items
                await conn.execute("DELETE FROM purchase_order_items WHERE purchase_order_id = $1", po_id)
                for item in new_items:
                    qty = item.get("quantity_ordered", 0) or 0
                    price = item.get("unit_price", 0) or 0
                    amount = qty * price
                    await conn.fetchrow(
                        """
                        INSERT INTO purchase_order_items (
                            purchase_order_id, ingredient_id, ingredient_name,
                            quantity_ordered, unit, unit_price, amount
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                        RETURNING *
                        """,
                        po_id, item.get("ingredient_id"), item.get("ingredient_name"),
                        qty, item.get("unit"), price, amount,
                    )
                sub_total = sum(
                    (it.get("quantity_ordered", 0) or 0) * (it.get("unit_price", 0) or 0)
                    for it in new_items
                )
                delivery_charges = fields.get("delivery_charges", existing["delivery_charges"]) or 0
                fields["sub_total"] = sub_total
                fields["total_amount"] = sub_total + delivery_charges

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

            # Fetch updated items
            items = await conn.fetch(
                "SELECT * FROM purchase_order_items WHERE purchase_order_id = $1 ORDER BY id",
                po_id,
            )
        result = dict(row)
        result["items"] = [dict(i) for i in items]
        return result

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
