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
    INVENTORY_NEGATIVE_STOCK, INVENTORY_LOW_STOCK,
    INVENTORY_ALERT_RAISED,
)
from app.core.exceptions import InventoryError, NotFoundError, ValidationError
from app.core.logging import get_logger
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
    ) -> str:
        """
        Append a single inventory event to `inventory_ledger` via the
        idempotent SQL function `fn_inventory_append_event`.

        Returns the canonical `event_id` (UUID).
        """
        if quantity_in == 0 and quantity_out == 0:
            raise ValidationError("inventory event requires non-zero quantity")

        ltype = ledger_type or EVENT_TO_LEDGER.get(event_type)
        if not ltype:
            raise ValidationError(f"unknown inventory event_type: {event_type}")

        import json
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _conn_ctx():
            if conn is not None:
                yield conn
            else:
                async with get_connection() as c:
                    yield c

        async with _conn_ctx() as conn:
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

            # Batch bookkeeping (FEFO consumption / receipt)
            if batch_id:
                await conn.execute(
                    """
                    UPDATE inventory_batches
                       SET remaining_quantity = GREATEST(remaining_quantity + $1, 0),
                           status = CASE
                               WHEN remaining_quantity + $1 <= 0 THEN 'depleted'
                               ELSE status
                           END,
                           updated_at = NOW()
                     WHERE id = $2::uuid
                    """,
                    float(delta), batch_id,
                )

        if publish:
            await emit_and_publish(DomainEvent(
                event_type=event_type,
                payload={
                    "event_id": str(event_id),
                    "ingredient_id": ingredient_id,
                    "branch_id": branch_id,
                    "quantity_in": float(quantity_in),
                    "quantity_out": float(quantity_out),
                    "unit_cost": float(unit_cost),
                    "reference_type": reference_type,
                    "reference_id": reference_id,
                    "correlation_id": correlation_id,
                    "batch_id": batch_id,
                    "source": source,
                },
                restaurant_id=restaurant_id,
                branch_id=branch_id,
                user_id=created_by,
                correlation_id=correlation_id,
            ))

        # Cache invalidation is best-effort; don't block the caller on a
        # Redis SCAN + per-key DELETE round-trip storm.
        import asyncio as _asyncio
        _asyncio.create_task(cache_delete_pattern(f"inv:bal:{ingredient_id}:*"))
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
        ltype = EVENT_TO_LEDGER.get(reversal_event_type, "restock_cancelled_order")
        async with get_connection() as conn:
            new_id = await conn.fetchval(
                "SELECT fn_inventory_reverse_event($1::uuid, $2, $3, $4)",
                original_event_id, ltype, notes, created_by,
            )
            row = await conn.fetchrow(
                """
                SELECT restaurant_id, branch_id, ingredient_id,
                       quantity_in, quantity_out
                  FROM inventory_ledger WHERE event_id = $1::uuid
                """,
                new_id,
            )
            if row:
                delta = Decimal(str(row["quantity_in"])) - Decimal(str(row["quantity_out"]))
                if delta != 0:
                    await conn.execute(
                        "UPDATE ingredients SET current_stock = COALESCE(current_stock,0)+$1, "
                        "stock_quantity = COALESCE(stock_quantity,0)+$1, updated_at=NOW() WHERE id=$2",
                        float(delta), row["ingredient_id"],
                    )
                await emit_and_publish(DomainEvent(
                    event_type=reversal_event_type,
                    payload={
                        "event_id": str(new_id),
                        "reverses_event": original_event_id,
                        "ingredient_id": row["ingredient_id"],
                        "branch_id": str(row["branch_id"]) if row["branch_id"] else None,
                    },
                    restaurant_id=str(row["restaurant_id"]) if row["restaurant_id"] else None,
                    branch_id=str(row["branch_id"]) if row["branch_id"] else None,
                    user_id=created_by,
                ))
        return str(new_id)

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
        correlation = order_id  # use order_id as correlation_id
        consumed: list[dict] = []
        low_stock_alerts: list[dict] = []

        async with get_serializable_transaction() as conn:
            # 1. Aggregate required quantities across all order items
            required: dict[str, Decimal] = {}
            for oi in order_items:
                item_id = oi.get("item_id")
                qty = Decimal(str(oi.get("quantity", 1)))
                if not item_id:
                    continue
                recipe = await self._resolve_recipe(
                    conn, restaurant_id=restaurant_id, item_id=item_id,
                )
                for line in recipe:
                    ing_id = line["ingredient_id"]
                    required[ing_id] = (
                        required.get(ing_id, Decimal("0"))
                        + line["qty_per_unit"] * qty
                    )

            if not required:
                return {"consumed": [], "skipped_no_recipe": True}

            # 2. Lock ingredient rows + verify availability
            ing_ids = list(required.keys())
            ing_rows = await conn.fetch(
                """
                SELECT id, name, unit, minimum_stock, reorder_point, cost_per_unit
                  FROM ingredients
                 WHERE id = ANY($1::text[])
                 FOR UPDATE
                """,
                ing_ids,
            )
            ing_meta = {r["id"]: dict(r) for r in ing_rows}

            for ing_id, need in required.items():
                meta = ing_meta.get(ing_id)
                if not meta:
                    continue
                # Live balance from ledger (branch-scoped if available)
                bal = await conn.fetchval(
                    "SELECT fn_inventory_balance($1, $2::uuid, NULL)",
                    ing_id, branch_id,
                )
                bal = Decimal(str(bal or 0))
                if bal < need and not allow_negative:
                    raise InventoryError(
                        f"insufficient stock for {meta['name']}: "
                        f"need {need}, have {bal}"
                    )

            # 3. Append one CONSUMED event per ingredient (idempotent)
            #    Reuse the same `conn` so the FK lock acquired by inventory_ledger
            #    INSERT (FOR KEY SHARE on ingredients) sees the outer FOR UPDATE
            #    lock as its own and doesn't block.
            for ing_id, need in required.items():
                meta = ing_meta.get(ing_id, {})
                dedup = f"order:{order_id}:ing:{ing_id}"
                event_id = await self.append_event(
                    restaurant_id=restaurant_id,
                    branch_id=branch_id,
                    ingredient_id=ing_id,
                    event_type=INVENTORY_CONSUMED,
                    quantity_out=need,
                    unit_cost=Decimal(str(meta.get("cost_per_unit") or 0)),
                    reference_type="order",
                    reference_id=order_id,
                    dedup_key=dedup,
                    correlation_id=correlation,
                    source="order",
                    notes=f"Order {order_id} consumption: {meta.get('name','')}",
                    created_by=user_id,
                    publish=False,  # one batched fan-out below
                    mirror_master=mirror_master,
                    conn=conn,
                )
                new_bal = await self.get_balance(ing_id, branch_id=branch_id)
                consumed.append({
                    "event_id": event_id,
                    "ingredient_id": ing_id,
                    "ingredient_name": meta.get("name"),
                    "consumed": float(need),
                    "remaining": float(new_bal),
                })
                # Low-stock detection
                min_stock = Decimal(str(meta.get("minimum_stock") or 0))
                reorder = Decimal(str(meta.get("reorder_point") or 0))
                threshold = max(min_stock, reorder)
                if threshold > 0 and new_bal <= threshold:
                    low_stock_alerts.append({
                        "ingredient_id": ing_id,
                        "ingredient_name": meta.get("name"),
                        "current": float(new_bal),
                        "threshold": float(threshold),
                    })

        # 4. Fan-out events
        await emit_and_publish(DomainEvent(
            event_type=INVENTORY_CONSUMED,
            payload={"order_id": order_id, "consumed": consumed},
            restaurant_id=restaurant_id, branch_id=branch_id,
            user_id=user_id, correlation_id=correlation,
        ))
        for alert in low_stock_alerts:
            await self._raise_low_stock_alert(
                restaurant_id=restaurant_id, branch_id=branch_id, alert=alert,
            )
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
