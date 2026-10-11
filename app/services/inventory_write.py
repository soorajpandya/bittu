"""Inventory transactions and unit normalization shared by stock writers."""
from contextlib import asynccontextmanager
from decimal import Decimal

from app.core.database import get_transaction
from app.core.events import emit_and_publish
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.core.redis import cache_delete_pattern

logger = get_logger(__name__)


async def publish_inventory_events(events):
    """Publish only after commit; notification failures must not replay a write."""
    for event in events:
        try:
            await emit_and_publish(event)
            ingredient = event.payload.get("ingredient_id")
            if ingredient:
                await cache_delete_pattern(f"inv:bal:{ingredient}:*")
        except Exception:
            logger.exception("inventory_notification_failed", event_type=event.event_type)


@asynccontextmanager
async def inventory_transaction():
    # All stock writers lock ingredient rows before availability checks. READ
    # COMMITTED then sees the preceding writer's committed balance after waiting.
    events = []
    async with get_transaction() as conn:
        yield conn, events
    await publish_inventory_events(events)


async def unit_factor(conn, *, restaurant_id, ingredient_id, from_unit, to_unit):
    """Convert recipe/purchase quantities to the ingredient's stock unit."""
    aliases = {"grams": "g", "gram": "g", "kilogram": "kg", "kilograms": "kg",
               "litre": "l", "liter": "l", "litres": "l", "liters": "l",
               "piece": "pcs", "pieces": "pcs", "pc": "pcs"}
    source = (from_unit or to_unit or "").strip().lower()
    target = (to_unit or "").strip().lower()
    source, target = aliases.get(source, source), aliases.get(target, target)
    if source == target:
        return Decimal("1")
    if not source or not target:
        raise ValidationError("both source and stock units are required")
    row = await conn.fetchrow(
        """SELECT from_unit, to_unit, factor FROM unit_conversions
           WHERE is_active = true
             AND (restaurant_id = $1::uuid OR restaurant_id IS NULL)
             AND (ingredient_id = $2 OR ingredient_id IS NULL)
             AND ((lower(from_unit) = $3 AND lower(to_unit) = $4)
               OR (lower(from_unit) = $4 AND lower(to_unit) = $3))
           ORDER BY restaurant_id NULLS LAST, ingredient_id NULLS LAST,
                    (lower(from_unit) = $3) DESC, id
           LIMIT 1""",
        restaurant_id, ingredient_id, source, target,
    )
    if row:
        factor = Decimal(str(row["factor"]))
        if factor <= 0:
            raise ValidationError("unit conversion factor must be positive")
        return factor if row["from_unit"].lower() == source else Decimal("1") / factor
    units = {"mg": ("mass", Decimal("0.001")), "g": ("mass", Decimal("1")),
             "kg": ("mass", Decimal("1000")), "ml": ("volume", Decimal("1")),
             "l": ("volume", Decimal("1000"))}
    if source in units and target in units and units[source][0] == units[target][0]:
        return units[source][1] / units[target][1]
    raise ValidationError(f"no unit conversion from {source} to {target} for {ingredient_id}")
