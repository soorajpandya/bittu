"""
InventoryEventService — event-sourced restaurant inventory engine.

Implements (per inventory.md):
  • SECTION 2 — Event System
        Every stock change is appended to `inventory_ledger` (immutable).
        Idempotent via `dedup_key`; correlated via `correlation_id`.
        All append/reverse paths emit a `DomainEvent` so the in-process
        bus + Redis pub/sub fan it out to ERP handlers, alert handlers,
        analytics aggregator, websocket subscribers.

  • SECTION 3 — Calculation Engine
        Live balance is `SUM(quantity_in) - SUM(quantity_out)` from the
        ledger, optionally as-of a timestamp / branch. A snapshot table
        (`inventory_snapshots`) is the cached "rolling" balance,
        rebuildable from the ledger at any point. The view
        `inventory_events` exposes a richer event-shaped projection.

  • SECTION 4 — Order Integration
        `consume_for_order()` resolves item → recipe → ingredients
        (preferring `recipes`/`recipe_ingredients`, falling back to the
        legacy `item_ingredients` table for backward compatibility) and
        appends one CONSUMED event per ingredient under a single
        correlation_id. Cancellation reverses every event of that
        correlation_id (RESTOCK_CANCELLED).

The service is purely additive — it does NOT replace
`InventoryService.deduct_for_order` (which still mutates `ingredients`
for the existing UI). The ERP event handler is updated to call BOTH so
nothing breaks while the new ledger becomes the source of truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Optional
from uuid import UUID

from app.core.database import get_connection, get_serializable_transaction
from app.core.events import (
    DomainEvent, emit_and_publish,
    INVENTORY_PURCHASED, INVENTORY_CONSUMED, INVENTORY_WASTED,
    INVENTORY_EXPIRED, INVENTORY_TRANSFERRED_OUT, INVENTORY_TRANSFERRED_IN,
    INVENTORY_ADJUSTED, INVENTORY_RECOUNTED, INVENTORY_RETURN_TO_VENDOR,
    INVENTORY_RESTOCK_CANCELLED, INVENTORY_OUT_OF_STOCK,
    INVENTORY_CONVERTED_IN, INVENTORY_CONVERTED_OUT, INVENTORY_SOLD,
    INVENTORY_NEGATIVE_STOCK, INVENTORY_LOW_STOCK,
    INVENTORY_ALERT_RAISED,
)
from app.core.exceptions import InventoryError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.services.inventory_write import inventory_transaction, unit_factor
from app.core.redis import cache_delete_pattern

logger = get_logger(__name__)


# ── Event-type → ledger transaction_type ─────────────────────────────────────

EVENT_TO_LEDGER = {
    INVENTORY_PURCHASED:           "purchase",
    INVENTORY_CONSUMED:            "consumption",
    INVENTORY_WASTED:              "wastage",
    INVENTORY_EXPIRED:             "expired",
    INVENTORY_TRANSFERRED_OUT:     "transfer_out",
    INVENTORY_TRANSFERRED_IN:      "transfer_in",
    INVENTORY_ADJUSTED:            "adjustment_in",   # caller picks _in/_out
    INVENTORY_RECOUNTED:           "recount",
    INVENTORY_RETURN_TO_VENDOR:    "return_to_vendor",
    INVENTORY_RESTOCK_CANCELLED:   "restock_cancelled_order",
    INVENTORY_CONVERTED_IN:        "conversion_in",
    INVENTORY_CONVERTED_OUT:       "conversion_out",
    INVENTORY_SOLD:                "sale",
}

@dataclass
class StockEvent:
    """Internal payload for one ingredient-level stock change."""
    ingredient_id: str
    quantity_in:   Decimal = Decimal("0")
    quantity_out:  Decimal = Decimal("0")
    unit_cost:     Decimal = Decimal("0")
    batch_id:      Optional[str] = None


class InventoryEventService:
    # ═════════════════════════════════════════════════════════════════════════
    # SECTION 2 — CORE EVENT APPEND  (idempotent, dual-emits domain events)
    # ═════════════════════════════════════════════════════════════════════════

    async def append_event(
        self,
        *,
        restaurant_id: str,
        branch_id: Optional[str],
        ingredient_id: str,
        event_type: str,
        quantity_in: Decimal | float | int = 0,
        quantity_out: Decimal | float | int = 0,
        unit_cost: Decimal | float | int = 0,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        dedup_key: Optional[str] = None,
        correlation_id: Optional[str] = None,
        batch_id: Optional[str] = None,
        source: str = "system",
        metadata: Optional[dict[str, Any]] = None,
        notes: Optional[str] = None,
        created_by: Optional[str] = None,
        ledger_type: Optional[str] = None,        # override for adjust_in/out
        publish: bool = True,
        mirror_master: bool = True,               # also mutate ingredients.current_stock
        conn=None,                                # reuse an outer transaction if provided
        event_buffer=None,                        # published by the owner after commit
    ) -> str:
        """
        Append a single inventory event to `inventory_ledger` via the
        idempotent SQL function `fn_inventory_append_event`.

        Returns the canonical `event_id` (UUID).
        """
        quantity_in, quantity_out = Decimal(str(quantity_in)), Decimal(str(quantity_out))
        if not quantity_in.is_finite() or not quantity_out.is_finite() or quantity_in < 0 or quantity_out < 0:
            raise ValidationError("inventory quantities must be finite and non-negative")
        if quantity_in > 0 and quantity_out > 0:
            raise ValidationError("an inventory event must have only one direction")
        if quantity_in == 0 and quantity_out == 0:
            raise ValidationError("inventory event requires non-zero quantity")

        ltype = ledger_type or EVENT_TO_LEDGER.get(event_type)
        if not ltype:
            raise ValidationError(f"unknown inventory event_type: {event_type}")

        import json
        from contextlib import asynccontextmanager

        owns_transaction = conn is None
        if not owns_transaction and publish and event_buffer is None:
            raise ValueError("outer inventory transactions must supply an event_buffer")

        @asynccontextmanager
        async def _conn_ctx():
            if conn is not None:
                yield conn, event_buffer
            else:
                async with inventory_transaction() as (c, events):
                    yield c, events

        async with _conn_ctx() as (conn, events):
            # Serialize the dedup check AND all derived stock mutations.
            ingredient = await conn.fetchrow(
                "SELECT id FROM ingredients WHERE id=$1 AND restaurant_id=$2::uuid FOR UPDATE",
                ingredient_id, restaurant_id,
            )
            if not ingredient:
                raise NotFoundError("ingredient", ingredient_id)
            if dedup_key:
                existing = await conn.fetchval(
                    "SELECT event_id FROM inventory_ledger WHERE restaurant_id=$1::uuid AND dedup_key=$2",
                    restaurant_id, dedup_key,
                )
                if existing:
                    return str(existing)
            event_id = await conn.fetchval(
                """
                SELECT fn_inventory_append_event(
                    $1::uuid, $2::uuid, $3, $4,
                    $5::numeric, $6::numeric, $7::numeric,
                    $8, $9, $10, $11::uuid, $12::uuid, $13, $14::jsonb,
                    $15, $16
                )
                """,
                restaurant_id, branch_id, ingredient_id, ltype,
                float(quantity_in), float(quantity_out), float(unit_cost),
                reference_type, reference_id, dedup_key,
                correlation_id, batch_id, source,
                json.dumps(metadata or {}),
                notes, created_by,
            )

            # Mirror the change onto `ingredients.current_stock` so legacy
            # readers (existing UI / endpoints) keep showing accurate values.
            # Pass mirror_master=False from callers that have already mutated
            # `ingredients` themselves (e.g. legacy InventoryService).
            delta = Decimal(str(quantity_in)) - Decimal(str(quantity_out))
            if delta != 0 and mirror_master:
                await conn.execute(
                    """
                    UPDATE ingredients
                       SET current_stock  = COALESCE(current_stock, 0)  + $1,
                           stock_quantity = COALESCE(stock_quantity, 0) + $1,
                           updated_at = NOW()
                     WHERE id = $2
                    """,
                    float(delta), ingredient_id,
                )

            # Batch updates and ledger writes commit or roll back together.
            if batch_id:
                updated = await conn.fetchval(
                    """UPDATE inventory_batches
                          SET remaining_quantity = remaining_quantity + $1,
                              status = CASE WHEN remaining_quantity + $1 = 0 THEN 'depleted'
                                            WHEN status = 'depleted' THEN 'active' ELSE status END,
                              updated_at = NOW()
                        WHERE id=$2::uuid AND ingredient_id=$3 AND restaurant_id=$4::uuid
                          AND branch_id IS NOT DISTINCT FROM $5::uuid
                          AND remaining_quantity + $1 >= 0
                        RETURNING id""",
                    delta, batch_id, ingredient_id, restaurant_id, branch_id,
                )
                if not updated:
                    raise InventoryError("batch not found in this branch or insufficient batch stock")

            if publish:
                events.append(DomainEvent(
                    event_type=event_type,
                    payload={
                        "event_id": str(event_id), "ingredient_id": ingredient_id,
                        "branch_id": branch_id, "quantity_in": float(quantity_in),
                        "quantity_out": float(quantity_out), "unit_cost": float(unit_cost),
                        "reference_type": reference_type, "reference_id": reference_id,
                        "correlation_id": correlation_id, "batch_id": batch_id, "source": source,
                    },
                    restaurant_id=restaurant_id, branch_id=branch_id,
                    user_id=created_by, correlation_id=correlation_id,
                ))
        return str(event_id)

    async def reverse_event(
        self,
        *,
        original_event_id: str,
        reversal_event_type: str = INVENTORY_RESTOCK_CANCELLED,
        notes: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> str:
        """Reverse a previously appended event (idempotent)."""
        async with inventory_transaction() as (conn, events):
            original = await conn.fetchrow(
                "SELECT * FROM inventory_ledger WHERE event_id=$1::uuid FOR UPDATE",
                original_event_id,
            )
            if not original:
                raise NotFoundError("inventory_event", original_event_id)
            if original["reversed_by"]:
                return str(original["reversed_by"])
            new_id = await self.append_event(
                conn=conn, event_buffer=events,
                restaurant_id=str(original["restaurant_id"]),
                branch_id=str(original["branch_id"]) if original["branch_id"] else None,
                ingredient_id=original["ingredient_id"], event_type=reversal_event_type,
                quantity_in=original["quantity_out"], quantity_out=original["quantity_in"],
                unit_cost=original["unit_cost"] or 0,
                batch_id=str(original["batch_id"]) if original["batch_id"] else None,
                reference_type=original["reference_type"], reference_id=original["reference_id"],
                correlation_id=str(original["correlation_id"]) if original["correlation_id"] else None,
                dedup_key=f"reversal:{original_event_id}", source="reversal",
                notes=notes, created_by=created_by,
            )
            await conn.execute(
                "UPDATE inventory_ledger SET reverses_event=$1::uuid WHERE event_id=$2::uuid",
                original_event_id, new_id,
            )
            await conn.execute(
                "UPDATE inventory_ledger SET reversed_by=$1::uuid WHERE event_id=$2::uuid",
                new_id, original_event_id,
            )
        return new_id

    # ═════════════════════════════════════════════════════════════════════════
    # SECTION 3 — CALCULATION ENGINE
    # ═════════════════════════════════════════════════════════════════════════

    async def get_balance(
        self, ingredient_id: str,
        branch_id: Optional[str] = None,
        as_of: Optional[str] = None,
    ) -> Decimal:
        """Compute the live balance from the immutable ledger."""
        async with get_connection() as conn:
            qty = await conn.fetchval(
                "SELECT fn_inventory_balance($1, $2::uuid, $3::timestamptz)",
                ingredient_id, branch_id, as_of,
            )
        return Decimal(str(qty or 0))

    async def get_balances_bulk(
        self, restaurant_id: str, branch_id: Optional[str] = None,
        ingredient_ids: Optional[list[str]] = None,
    ) -> list[dict]:
        """Vectorised balance lookup across many ingredients."""
        params: list = [restaurant_id]
        where = "i.restaurant_id = $1 AND COALESCE(i.deleted_at::text,'') = ''"
        if branch_id:
            params.append(branch_id)
            where += f" AND (i.branch_id = ${len(params)} OR i.branch_id IS NULL)"
            branch_filter = f" AND (l.branch_id = ${len(params)} OR l.branch_id IS NULL)"
        else:
            branch_filter = ""
        if ingredient_ids:
            params.append(ingredient_ids)
            where += f" AND i.id = ANY(${len(params)})"

        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""
                SELECT i.id          AS ingredient_id,
                       i.name,
                       i.unit,
                       i.minimum_stock,
                       i.reorder_point,
                       i.cost_per_unit,
                       COALESCE(SUM(l.quantity_in - l.quantity_out), 0) AS balance
                  FROM ingredients i
                  LEFT JOIN inventory_ledger l ON l.ingredient_id = i.id {branch_filter}
                 WHERE {where}
                 GROUP BY i.id, i.name, i.unit, i.minimum_stock, i.reorder_point, i.cost_per_unit
                 ORDER BY i.name
                """,
                *params,
            )
        return [dict(r) for r in rows]

    async def build_snapshot(
        self, *, restaurant_id: str,
        branch_id: Optional[str] = None,
        period: str = "rolling",
    ) -> int:
        """Materialise inventory_snapshots from the ledger. Returns row count."""
        async with get_serializable_transaction() as conn:
            ingredients = await conn.fetch(
                "SELECT id FROM ingredients WHERE restaurant_id = $1::uuid",
                restaurant_id,
            )
            count = 0
            for ing in ingredients:
                row = await conn.fetchrow(
                    """
                    SELECT COALESCE(SUM(quantity_in),0)  AS in_qty,
                           COALESCE(SUM(quantity_out),0) AS out_qty,
                           (
                               SELECT event_id FROM inventory_ledger
                                WHERE ingredient_id = $1
                                  AND ($2::uuid IS NULL OR branch_id = $2::uuid)
                                ORDER BY occurred_at DESC
                                LIMIT 1
                           )                              AS last_event
                      FROM inventory_ledger
                     WHERE ingredient_id = $1
                       AND ($2::uuid IS NULL OR branch_id = $2::uuid)
                    """,
                    ing["id"], branch_id,
                )
                in_qty = Decimal(str(row["in_qty"]))
                out_qty = Decimal(str(row["out_qty"]))
                closing = in_qty - out_qty

                cost_row = await conn.fetchrow(
                    """
                    SELECT COALESCE(AVG(NULLIF(unit_cost,0)),0) AS avg_cost
                      FROM inventory_ledger
                     WHERE ingredient_id = $1
                       AND transaction_type = 'purchase'
                    """,
                    ing["id"],
                )
                avg_cost = Decimal(str(cost_row["avg_cost"] or 0))

                await conn.execute(
                    """
                    INSERT INTO inventory_snapshots
                        (restaurant_id, branch_id, ingredient_id, period,
                         opening_qty, in_qty, out_qty, closing_qty,
                         avg_unit_cost, valuation, last_event_id)
                    VALUES ($1::uuid,$2::uuid,$3,$4,0,$5,$6,$7,$8,$9,$10::uuid)
                    ON CONFLICT (restaurant_id, branch_id, ingredient_id, period, snapshot_at)
                    DO NOTHING
                    """,
                    restaurant_id, branch_id, ing["id"], period,
                    float(in_qty), float(out_qty), float(closing),
                    float(avg_cost), float(closing * avg_cost),
                    row["last_event"],
                )
                count += 1
        return count

    async def timeline(
        self, *, ingredient_id: str,
        branch_id: Optional[str] = None,
        limit: int = 100, offset: int = 0,
    ) -> list[dict]:
        """Per-ingredient inventory timeline (event view, paginated)."""
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT event_id, event_type, quantity_in, quantity_out, delta,
                       unit_cost, value_delta, reference_type, reference_id,
                       correlation_id, batch_id, source, notes, created_by, occurred_at
                  FROM inventory_events
                 WHERE ingredient_id = $1
                   AND ($2::uuid IS NULL OR branch_id = $2::uuid)
                 ORDER BY occurred_at DESC
                 LIMIT $3 OFFSET $4
                """,
                ingredient_id, branch_id, limit, offset,
            )
        return [dict(r) for r in rows]

    # ═════════════════════════════════════════════════════════════════════════
    # SECTION 4 — ORDER INTEGRATION
    # ═════════════════════════════════════════════════════════════════════════

    async def _resolve_recipe(
        self, conn, *, restaurant_id: Optional[str], item_id: int | str,
    ) -> list[dict]:
        """
        Resolve `item_id` → ingredient consumption list.
        Prefers `recipes`/`recipe_ingredients`; falls back to legacy
        `item_ingredients` so existing seeded menus still consume stock.
        """
        if restaurant_id:
            rows = await conn.fetch(
                """
                SELECT ri.ingredient_id,
                       ri.quantity_required AS qty,
                       ri.unit,
                       COALESCE(ri.waste_percent,0) AS waste_pct,
                       COALESCE(r.yield_quantity,1) AS yield_qty
                  FROM recipes r
                  JOIN recipe_ingredients ri ON ri.recipe_id = r.id
                 WHERE r.restaurant_id = $1::uuid
                   AND r.item_id = $2
                   AND r.is_active = true
                """,
                restaurant_id, int(item_id),
            )
            if rows:
                return [
                    {
                        "ingredient_id": r["ingredient_id"],
                        "qty_per_unit": (
                            Decimal(str(r["qty"]))
                            * (Decimal("1") + Decimal(str(r["waste_pct"])) / Decimal("100"))
                            / Decimal(str(r["yield_qty"] or 1))
                        ),
                        "unit": r["unit"],
                    }
                    for r in rows
                ]
        # Legacy fallback
        rows = await conn.fetch(
            """
            SELECT ii.ingredient_id, ii.quantity_used AS qty, ii.unit
              FROM item_ingredients ii
             WHERE ii.item_id = $1
            """,
            int(item_id),
        )
        return [
            {"ingredient_id": r["ingredient_id"],
             "qty_per_unit": Decimal(str(r["qty"])),
             "unit": r["unit"]}
            for r in rows
        ]

    async def consume_for_order(
        self, *, restaurant_id: Optional[str], branch_id: Optional[str],
        order_id: str, order_items: Iterable[dict],
        user_id: Optional[str] = None,
        allow_negative: bool = False,
        mirror_master: bool = True,
    ) -> dict:
        """
        Append CONSUMED events for every ingredient required by `order_items`.

        order_items: [{item_id, quantity}]
        Idempotent per (order_id, ingredient_id) via dedup_key.
        Raises InventoryError if any ingredient would go negative
        (unless `allow_negative=True`).
        """
        consumed: list[dict] = []
        low_stock_alerts: list[dict] = []
        async with inventory_transaction() as (conn, events):
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                               f"inventory:order:{order_id}")
            # An order's entire consumption is atomic, including all batch splits.
            # Check before availability so a retry succeeds even if stock ran out.
            existing = await conn.fetch(
                "SELECT event_id, ingredient_id, quantity_out FROM inventory_ledger "
                "WHERE restaurant_id=$1::uuid AND reference_type='order' "
                "AND reference_id=$2 AND transaction_type='consumption'",
                restaurant_id, order_id,
            )
            if existing:
                return {"consumed": [dict(r) for r in existing], "already_consumed": True}

            lines = []
            for oi in order_items:
                qty = Decimal(str(oi.get("quantity", 1)))
                if qty <= 0:
                    raise ValidationError("order quantities must be positive")
                if not oi.get("item_id"):
                    continue
                recipe = await self._resolve_recipe(conn, restaurant_id=restaurant_id,
                                                    item_id=oi["item_id"])
                lines.extend({**line, "required": line["qty_per_unit"] * qty} for line in recipe)
            if not lines:
                return {"consumed": [], "skipped_no_recipe": True}
            ing_ids = sorted({line["ingredient_id"] for line in lines})
            rows = await conn.fetch(
                "SELECT id, name, unit, minimum_stock, reorder_point, cost_per_unit, track_batches "
                "FROM ingredients WHERE id=ANY($1::text[]) AND restaurant_id=$2::uuid "
                "ORDER BY id FOR UPDATE", ing_ids, restaurant_id,
            )
            ingredients = {row["id"]: dict(row) for row in rows}
            required = {}
            for line in lines:
                ing_id = line["ingredient_id"]
                if ing_id not in ingredients:
                    raise NotFoundError("ingredient", ing_id)
                factor = await unit_factor(conn, restaurant_id=restaurant_id, ingredient_id=ing_id,
                                           from_unit=line["unit"], to_unit=ingredients[ing_id]["unit"])
                need = line["required"] * factor
                if need <= 0:
                    raise ValidationError("recipe quantities must be positive")
                required[ing_id] = required.get(ing_id, Decimal("0")) + need

            for ing_id in ing_ids:
                need = required[ing_id]
                meta = ingredients[ing_id]
                bal = Decimal(str(await conn.fetchval(
                    "SELECT fn_inventory_balance($1, $2::uuid, NULL)", ing_id, branch_id,
                ) or 0))
                if bal < need and not allow_negative:
                    raise InventoryError(f"insufficient stock for {meta['name']}: need {need}, have {bal}")
                allocations = [(None, need, Decimal(str(meta["cost_per_unit"] or 0)))]
                if meta["track_batches"]:
                    batches = await conn.fetch(
                        """SELECT id, remaining_quantity, unit_cost, unit FROM inventory_batches
                           WHERE restaurant_id=$1::uuid AND ingredient_id=$2
                             AND branch_id IS NOT DISTINCT FROM $3::uuid
                             AND status='active' AND remaining_quantity > 0
                             AND (expiry_date IS NULL OR expiry_date >= CURRENT_DATE)
                           ORDER BY expiry_date ASC NULLS LAST, received_date, id FOR UPDATE""",
                        restaurant_id, ing_id, branch_id,
                    )
                    allocations = []
                    remaining = need
                    for batch in batches:
                        if remaining <= 0:
                            break
                        factor = await unit_factor(conn, restaurant_id=restaurant_id, ingredient_id=ing_id,
                                                   from_unit=batch["unit"], to_unit=meta["unit"])
                        # Batch quantities are normalized to the stock unit at receipt.
                        if factor != 1:
                            raise ValidationError("batch unit must match ingredient stock unit")
                        take = min(remaining, Decimal(str(batch["remaining_quantity"])))
                        allocations.append((str(batch["id"]), take, Decimal(str(batch["unit_cost"] or 0))))
                        remaining -= take
                    if remaining > 0:
                        raise InventoryError(f"insufficient unexpired batch stock for {meta['name']}")
                event_ids = []
                for batch_id, quantity, cost in allocations:
                    event_ids.append(await self.append_event(
                        conn=conn, event_buffer=events,
                        restaurant_id=restaurant_id, branch_id=branch_id, ingredient_id=ing_id,
                        event_type=INVENTORY_CONSUMED, quantity_out=quantity, unit_cost=cost,
                        batch_id=batch_id, reference_type="order", reference_id=order_id,
                        dedup_key=f"order:{order_id}:ing:{ing_id}:batch:{batch_id or 'none'}",
                        correlation_id=order_id, source="order", created_by=user_id,
                        mirror_master=mirror_master,
                    ))
                # Read our own writes, before commit, on the same connection.
                new_bal = Decimal(str(await conn.fetchval(
                    "SELECT fn_inventory_balance($1, $2::uuid, NULL)", ing_id, branch_id,
                ) or 0))
                consumed.append({"event_id": event_ids[0], "event_ids": event_ids,
                                 "ingredient_id": ing_id, "ingredient_name": meta["name"],
                                 "consumed": float(need), "remaining": float(new_bal)})
                threshold = max(Decimal(str(meta["minimum_stock"] or 0)),
                                Decimal(str(meta["reorder_point"] or 0)))
                if new_bal <= threshold:
                    low_stock_alerts.append({"ingredient_id": ing_id, "ingredient_name": meta["name"],
                                             "current": float(new_bal), "threshold": float(threshold)})
        for alert in low_stock_alerts:
            await self._raise_low_stock_alert(restaurant_id=restaurant_id, branch_id=branch_id, alert=alert)
        return {"consumed": consumed, "low_stock": low_stock_alerts}

    async def restock_cancelled_order(
        self, *, restaurant_id: Optional[str], branch_id: Optional[str],
        order_id: str, user_id: Optional[str] = None,
    ) -> dict:
        """Reverse every CONSUMED event tied to this order_id."""
        async with get_connection() as conn:
            originals = await conn.fetch(
                """
                SELECT event_id, ingredient_id
                  FROM inventory_ledger
                 WHERE correlation_id = $1::uuid
                   AND transaction_type = 'consumption'
                   AND reversed_by IS NULL
                """,
                order_id,
            )
        restored = []
        for ev in originals:
            new_id = await self.reverse_event(
                original_event_id=str(ev["event_id"]),
                reversal_event_type=INVENTORY_RESTOCK_CANCELLED,
                notes=f"Order {order_id} cancelled — restock",
                created_by=user_id,
            )
            restored.append({"reversed": str(ev["event_id"]), "by": new_id,
                             "ingredient_id": ev["ingredient_id"]})

        await emit_and_publish(DomainEvent(
            event_type=INVENTORY_RESTOCK_CANCELLED,
            payload={"order_id": order_id, "restored": restored},
            restaurant_id=restaurant_id, branch_id=branch_id, user_id=user_id,
        ))
        return {"restored": restored}

    # ═════════════════════════════════════════════════════════════════════════
    # ALERT HELPERS
    # ═════════════════════════════════════════════════════════════════════════

    async def _raise_low_stock_alert(
        self, *, restaurant_id: Optional[str],
        branch_id: Optional[str], alert: dict,
    ):
        if not restaurant_id:
            return
        async with get_connection() as conn:
            existing = await conn.fetchval(
                """
                SELECT id FROM inventory_alerts
                 WHERE restaurant_id = $1::uuid
                   AND ingredient_id = $2
                   AND alert_type IN ('low_stock','out_of_stock')
                   AND status = 'open'
                 LIMIT 1
                """,
                restaurant_id, alert["ingredient_id"],
            )
            if existing:
                return  # do not spam
            atype = "out_of_stock" if alert["current"] <= 0 else "low_stock"
            severity = "critical" if alert["current"] <= 0 else "warning"
            import json
            await conn.execute(
                """
                INSERT INTO inventory_alerts
                    (restaurant_id, branch_id, ingredient_id,
                     alert_type, severity, title, message, payload)
                VALUES ($1::uuid,$2::uuid,$3,$4,$5,$6,$7,$8::jsonb)
                """,
                restaurant_id, branch_id, alert["ingredient_id"],
                atype, severity,
                f"Low stock: {alert.get('ingredient_name') or alert['ingredient_id']}",
                f"Current {alert['current']} ≤ threshold {alert['threshold']}",
                json.dumps(alert),
            )
        await emit_and_publish(DomainEvent(
            event_type=INVENTORY_LOW_STOCK,
            payload=alert,
            restaurant_id=restaurant_id, branch_id=branch_id,
        ))
        await emit_and_publish(DomainEvent(
            event_type=INVENTORY_ALERT_RAISED,
            payload={"alert_type": atype, **alert},
            restaurant_id=restaurant_id, branch_id=branch_id,
        ))


# Module-level singleton
inventory_event_service = InventoryEventService()
