"""Inventory conversion service — semi-finished goods production.

Petpooja "Conversion": consume raw materials to produce an intermediate
(semi-finished) good, e.g. rice + water + oil + salt → dosa batter. The
produced good is itself an ``ingredients`` row (``is_semi_finished = true``)
that recipes can then consume like any other ingredient.

Every conversion appends, under a single ``correlation_id``:
  • one ``conversion_out`` event per raw-material input (stock decreases)
  • one ``conversion_in`` event for the produced output (stock increases)

so the whole production run is auditable and reversible via the ledger.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from app.core.database import get_connection, get_serializable_transaction
from app.core.events import INVENTORY_CONVERTED_IN, INVENTORY_CONVERTED_OUT
from app.core.exceptions import InventoryError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.services.inventory_event_service import inventory_event_service

logger = get_logger(__name__)


class InventoryConversionService:

    # ── Conversion recipes (masters) ────────────────────────────────────────

    async def create_recipe(
        self, *, restaurant_id: str, branch_id: Optional[str],
        output_ingredient_id: str, yield_quantity: Decimal, yield_unit: Optional[str],
        inputs: list[dict], name: Optional[str] = None, notes: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> dict:
        if yield_quantity <= 0:
            raise ValidationError("yield_quantity must be positive")
        if not inputs:
            raise ValidationError("at least one input ingredient is required")

        async with get_serializable_transaction() as conn:
            out = await conn.fetchrow(
                "SELECT id FROM ingredients WHERE id=$1 AND restaurant_id=$2::uuid",
                output_ingredient_id, restaurant_id,
            )
            if not out:
                raise NotFoundError("ingredient", output_ingredient_id)

            recipe_id = await conn.fetchval(
                """
                INSERT INTO conversion_recipes
                    (restaurant_id, branch_id, output_ingredient_id, name,
                     yield_quantity, yield_unit, notes, created_by)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8)
                RETURNING id
                """,
                restaurant_id, branch_id, output_ingredient_id, name,
                float(yield_quantity), yield_unit, notes, created_by,
            )
            for line in inputs:
                await conn.execute(
                    """
                    INSERT INTO conversion_recipe_inputs
                        (conversion_recipe_id, ingredient_id, quantity_required,
                         unit, waste_percent)
                    VALUES ($1::uuid, $2, $3, $4, $5)
                    """,
                    recipe_id, line["ingredient_id"],
                    float(Decimal(str(line["quantity_required"]))),
                    line.get("unit"),
                    float(Decimal(str(line.get("waste_percent", 0) or 0))),
                )
        return await self.get_recipe(restaurant_id=restaurant_id, recipe_id=str(recipe_id))

    async def get_recipe(self, *, restaurant_id: str, recipe_id: str) -> dict:
        async with get_connection() as conn:
            r = await conn.fetchrow(
                "SELECT * FROM conversion_recipes "
                "WHERE id=$1::uuid AND restaurant_id=$2::uuid AND deleted_at IS NULL",
                recipe_id, restaurant_id,
            )
            if not r:
                raise NotFoundError("conversion_recipe", recipe_id)
            inputs = await conn.fetch(
                "SELECT * FROM conversion_recipe_inputs WHERE conversion_recipe_id=$1::uuid",
                recipe_id,
            )
        result = dict(r)
        result["inputs"] = [dict(i) for i in inputs]
        return result

    async def list_recipes(
        self, *, restaurant_id: str, output_ingredient_id: Optional[str] = None,
    ) -> list[dict]:
        params: list[Any] = [restaurant_id]
        where = "restaurant_id = $1::uuid AND deleted_at IS NULL"
        if output_ingredient_id:
            params.append(output_ingredient_id)
            where += f" AND output_ingredient_id = ${len(params)}"
        async with get_connection() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM conversion_recipes WHERE {where} "
                f"ORDER BY created_at DESC",
                *params,
            )
        return [dict(r) for r in rows]

    # ── Conversion runs (production) ────────────────────────────────────────

    async def _resolve_inputs(
        self, conn, *, restaurant_id: str, conversion_recipe_id: Optional[str],
        produced_quantity: Decimal, explicit_inputs: Optional[list[dict]],
    ) -> list[dict]:
        """Return [{ingredient_id, qty}] scaled to the produced quantity."""
        if conversion_recipe_id:
            r = await conn.fetchrow(
                "SELECT yield_quantity FROM conversion_recipes "
                "WHERE id=$1::uuid AND restaurant_id=$2::uuid AND deleted_at IS NULL",
                conversion_recipe_id, restaurant_id,
            )
            if not r:
                raise NotFoundError("conversion_recipe", conversion_recipe_id)
            yield_qty = Decimal(str(r["yield_quantity"] or 1)) or Decimal("1")
            rows = await conn.fetch(
                "SELECT ingredient_id, quantity_required, waste_percent "
                "FROM conversion_recipe_inputs WHERE conversion_recipe_id=$1::uuid",
                conversion_recipe_id,
            )
            scale = produced_quantity / yield_qty
            return [
                {
                    "ingredient_id": row["ingredient_id"],
                    "qty": (
                        Decimal(str(row["quantity_required"]))
                        * (Decimal("1") + Decimal(str(row["waste_percent"] or 0)) / Decimal("100"))
                        * scale
                    ),
                }
                for row in rows
            ]
        if not explicit_inputs:
            raise ValidationError("conversion_recipe_id or inputs required")
        return [
            {
                "ingredient_id": line["ingredient_id"],
                "qty": Decimal(str(line["quantity"])),
            }
            for line in explicit_inputs
        ]

    async def convert(
        self, *, restaurant_id: str, branch_id: Optional[str],
        output_ingredient_id: str, produced_quantity: Decimal,
        conversion_recipe_id: Optional[str] = None,
        inputs: Optional[list[dict]] = None,
        output_unit: Optional[str] = None,
        notes: Optional[str] = None, created_by: Optional[str] = None,
        allow_negative: bool = False,
    ) -> dict:
        if produced_quantity <= 0:
            raise ValidationError("produced_quantity must be positive")

        async with get_serializable_transaction() as conn:
            out = await conn.fetchrow(
                "SELECT id, name, cost_per_unit FROM ingredients "
                "WHERE id=$1 AND restaurant_id=$2::uuid",
                output_ingredient_id, restaurant_id,
            )
            if not out:
                raise NotFoundError("ingredient", output_ingredient_id)

            resolved = await self._resolve_inputs(
                conn, restaurant_id=restaurant_id,
                conversion_recipe_id=conversion_recipe_id,
                produced_quantity=produced_quantity, explicit_inputs=inputs,
            )
            if not resolved:
                raise ValidationError("conversion has no input ingredients")

            # Verify availability
            input_cost = Decimal("0")
            for line in resolved:
                bal = await conn.fetchval(
                    "SELECT fn_inventory_balance($1, $2::uuid, NULL)",
                    line["ingredient_id"], branch_id,
                )
                bal = Decimal(str(bal or 0))
                if bal < line["qty"] and not allow_negative:
                    raise InventoryError(
                        f"insufficient stock for input {line['ingredient_id']}: "
                        f"need {line['qty']}, have {bal}"
                    )
                cpu = await conn.fetchval(
                    "SELECT cost_per_unit FROM ingredients WHERE id=$1",
                    line["ingredient_id"],
                )
                input_cost += Decimal(str(cpu or 0)) * line["qty"]

            conv_id = await conn.fetchval(
                """
                INSERT INTO inventory_conversions
                    (restaurant_id, branch_id, conversion_recipe_id,
                     output_ingredient_id, produced_quantity, output_unit,
                     notes, created_by)
                VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8)
                RETURNING id
                """,
                restaurant_id, branch_id, conversion_recipe_id,
                output_ingredient_id, float(produced_quantity), output_unit,
                notes, created_by,
            )

        correlation = str(conv_id)
        # Consume inputs
        for line in resolved:
            await inventory_event_service.append_event(
                restaurant_id=restaurant_id, branch_id=branch_id,
                ingredient_id=line["ingredient_id"],
                event_type=INVENTORY_CONVERTED_OUT,
                quantity_out=line["qty"],
                reference_type="inventory_conversion",
                reference_id=correlation,
                dedup_key=f"conversion:{correlation}:out:{line['ingredient_id']}",
                correlation_id=correlation,
                source="conversion",
                notes=f"Conversion {correlation} input",
                created_by=created_by,
            )

        # Produce output (unit cost = total input cost / produced qty)
        out_unit_cost = (
            input_cost / produced_quantity if produced_quantity else Decimal("0")
        )
        await inventory_event_service.append_event(
            restaurant_id=restaurant_id, branch_id=branch_id,
            ingredient_id=output_ingredient_id,
            event_type=INVENTORY_CONVERTED_IN,
            quantity_in=produced_quantity,
            unit_cost=out_unit_cost,
            reference_type="inventory_conversion",
            reference_id=correlation,
            dedup_key=f"conversion:{correlation}:in",
            correlation_id=correlation,
            source="conversion",
            notes=f"Conversion {correlation} output: {out['name']}",
            created_by=created_by,
        )

        return {
            "conversion_id": correlation,
            "output_ingredient_id": output_ingredient_id,
            "produced_quantity": float(produced_quantity),
            "inputs_consumed": [
                {"ingredient_id": l["ingredient_id"], "quantity": float(l["qty"])}
                for l in resolved
            ],
            "output_unit_cost": float(out_unit_cost),
        }

    async def list_conversions(
        self, *, restaurant_id: str, limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        async with get_connection() as conn:
            rows = await conn.fetch(
                "SELECT * FROM inventory_conversions WHERE restaurant_id=$1::uuid "
                "ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                restaurant_id, limit, offset,
            )
        return [dict(r) for r in rows]


inventory_conversion_service = InventoryConversionService()
