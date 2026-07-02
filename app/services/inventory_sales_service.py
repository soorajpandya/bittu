"""Inventory raw-material sales service.

Petpooja "Sales": an outlet sells raw materials (not dishes) to another
outlet or party. Each confirmed sale deducts the sold quantity from stock
via a ``sale`` ledger event, and records an invoice header + line items.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from app.core.database import get_connection, get_serializable_transaction
from app.core.events import INVENTORY_SOLD
from app.core.exceptions import InventoryError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.services.inventory_event_service import inventory_event_service

logger = get_logger(__name__)

_Q2 = Decimal("0.01")


def _money(x) -> Decimal:
    return Decimal(str(x or 0)).quantize(_Q2, rounding=ROUND_HALF_UP)


class InventorySalesService:

    async def create_sale(
        self, *, restaurant_id: str, branch_id: Optional[str],
        items: list[dict], buyer_name: Optional[str] = None,
        buyer_gst: Optional[str] = None, buyer_contact: Optional[str] = None,
        buyer_address: Optional[str] = None, terms: Optional[str] = None,
        notes: Optional[str] = None, created_by: Optional[str] = None,
        allow_negative: bool = False,
    ) -> dict:
        if not items:
            raise ValidationError("at least one sale item is required")

        # Compute per-line + totals
        sub_total = Decimal("0")
        tax_amount = Decimal("0")
        prepared: list[dict] = []
        for it in items:
            qty = Decimal(str(it["quantity"]))
            if qty <= 0:
                raise ValidationError("sale item quantity must be positive")
            price = Decimal(str(it.get("unit_price", 0) or 0))
            tax_pct = Decimal(str(it.get("tax_percent", 0) or 0))
            line_net = qty * price
            line_tax = line_net * tax_pct / Decimal("100")
            sub_total += line_net
            tax_amount += line_tax
            prepared.append({
                "ingredient_id": it["ingredient_id"],
                "quantity": qty,
                "unit": it.get("unit"),
                "unit_price": price,
                "tax_percent": tax_pct,
                "line_total": _money(line_net + line_tax),
            })

        total = _money(sub_total + tax_amount)

        async with get_serializable_transaction() as conn:
            # Verify each ingredient exists & has stock
            for line in prepared:
                ing = await conn.fetchrow(
                    "SELECT id FROM ingredients WHERE id=$1 AND restaurant_id=$2::uuid",
                    line["ingredient_id"], restaurant_id,
                )
                if not ing:
                    raise NotFoundError("ingredient", line["ingredient_id"])
                bal = await conn.fetchval(
                    "SELECT fn_inventory_balance($1, $2::uuid, NULL)",
                    line["ingredient_id"], branch_id,
                )
                bal = Decimal(str(bal or 0))
                if bal < line["quantity"] and not allow_negative:
                    raise InventoryError(
                        f"insufficient stock to sell {line['ingredient_id']}: "
                        f"need {line['quantity']}, have {bal}"
                    )

            sale = await conn.fetchrow(
                """
                INSERT INTO inventory_sales
                    (restaurant_id, branch_id, buyer_name, buyer_gst,
                     buyer_contact, buyer_address, sub_total, tax_amount,
                     total_amount, terms, notes, created_by)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                RETURNING *
                """,
                restaurant_id, branch_id, buyer_name, buyer_gst,
                buyer_contact, buyer_address, float(_money(sub_total)),
                float(_money(tax_amount)), float(total), terms, notes, created_by,
            )
            sale_id = sale["id"]
            for line in prepared:
                await conn.execute(
                    """
                    INSERT INTO inventory_sale_items
                        (sale_id, ingredient_id, quantity, unit, unit_price,
                         tax_percent, line_total)
                    VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)
                    """,
                    sale_id, line["ingredient_id"], float(line["quantity"]),
                    line["unit"], float(line["unit_price"]),
                    float(line["tax_percent"]), float(line["line_total"]),
                )

        correlation = str(sale_id)
        for line in prepared:
            await inventory_event_service.append_event(
                restaurant_id=restaurant_id, branch_id=branch_id,
                ingredient_id=line["ingredient_id"],
                event_type=INVENTORY_SOLD,
                quantity_out=line["quantity"],
                unit_cost=line["unit_price"],
                reference_type="inventory_sale",
                reference_id=correlation,
                dedup_key=f"sale:{correlation}:{line['ingredient_id']}",
                correlation_id=correlation,
                source="sale",
                notes=f"Raw-material sale {sale['sale_number']}",
                created_by=created_by,
            )

        return await self.get_sale(restaurant_id=restaurant_id, sale_id=correlation)

    async def get_sale(self, *, restaurant_id: str, sale_id: str) -> dict:
        async with get_connection() as conn:
            sale = await conn.fetchrow(
                "SELECT * FROM inventory_sales WHERE id=$1::uuid AND restaurant_id=$2::uuid",
                sale_id, restaurant_id,
            )
            if not sale:
                raise NotFoundError("inventory_sale", sale_id)
            items = await conn.fetch(
                """
                SELECT si.*, i.name AS ingredient_name
                  FROM inventory_sale_items si
                  LEFT JOIN ingredients i ON i.id = si.ingredient_id
                 WHERE si.sale_id = $1::uuid
                 ORDER BY si.id
                """,
                sale_id,
            )
        result = dict(sale)
        result["items"] = [dict(i) for i in items]
        return result

    async def list_sales(
        self, *, restaurant_id: str, status: Optional[str] = None,
        limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        params: list[Any] = [restaurant_id]
        where = "restaurant_id = $1::uuid"
        if status:
            params.append(status)
            where += f" AND status = ${len(params)}"
        params.extend([limit, offset])
        async with get_connection() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM inventory_sales WHERE {where} "
                f"ORDER BY created_at DESC LIMIT ${len(params)-1} OFFSET ${len(params)}",
                *params,
            )
        return [dict(r) for r in rows]


inventory_sales_service = InventorySalesService()
