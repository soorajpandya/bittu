"""Ingredients endpoints — list / create / update / soft-delete.

Ingredients are the inventory master rows. Once an ingredient exists it
shows up automatically in the event-sourced inventory module
(`/api/v1/inventory/balances`).

The Inventory dashboard uses POST here to add stock items manually.
If `current_stock > 0`, an `opening` ledger event is appended so the
event-sourced balance equals the master from second one.

All writes are idempotent on `(restaurant_id, lower(name))`.
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, get_current_user, require_permission
from app.core.database import get_connection
from app.core.logging import get_logger

router = APIRouter(prefix="/ingredients", tags=["Inventory"])
logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────

class IngredientIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    unit: str = Field("unit", max_length=20)
    current_stock: float = 0
    reorder_point: Optional[float] = None
    reorder_quantity: Optional[float] = None
    minimum_stock: Optional[float] = None
    cost_per_unit: Optional[float] = None
    category: Optional[str] = None
    storage_location: Optional[str] = None
    storage_type: Optional[str] = "dry"
    is_perishable: bool = False
    shelf_life_days: Optional[int] = None
    track_batches: bool = False
    sku: Optional[str] = None
    barcode: Optional[str] = None
    supplier: Optional[str] = None
    branch_id: Optional[str] = None


class IngredientPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    unit: Optional[str] = None
    reorder_point: Optional[float] = None
    reorder_quantity: Optional[float] = None
    minimum_stock: Optional[float] = None
    cost_per_unit: Optional[float] = None
    category: Optional[str] = None
    storage_location: Optional[str] = None
    storage_type: Optional[str] = None
    is_perishable: Optional[bool] = None
    shelf_life_days: Optional[int] = None
    track_batches: Optional[bool] = None
    sku: Optional[str] = None
    barcode: Optional[str] = None
    supplier: Optional[str] = None
    is_active: Optional[bool] = None
    branch_id: Optional[str] = None


def _owner_id(user: UserContext) -> str:
    return user.owner_id if user.is_branch_user else user.user_id


# ─────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────

@router.get("")
async def list_ingredients(
    include_inactive: bool = Query(False),
    user: UserContext = Depends(get_current_user),
):
    """List all ingredients for the current user's restaurant."""
    try:
        owner_id = _owner_id(user)
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM ingredients
                 WHERE user_id = $1
                   AND deleted_at IS NULL
                   AND ($2::boolean OR is_active = TRUE)
                 ORDER BY name
                """,
                owner_id, include_inactive,
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("list_ingredients_failed", error=str(e), user_id=user.user_id)
        return []


@router.post("", status_code=201)
async def create_ingredient(
    body: IngredientIn,
    user: UserContext = Depends(require_permission("menu.write")),
):
    """Create an ingredient manually from the Inventory UI.

    Idempotent on `(restaurant_id, lower(name))` — re-POSTing the same
    name returns the existing row instead of erroring.
    """
    owner_id = _owner_id(user)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name required")

    async with get_connection() as conn:
        existing = await conn.fetchrow(
            """
            SELECT * FROM ingredients
             WHERE user_id = $1 AND lower(name) = lower($2)
               AND deleted_at IS NULL
             LIMIT 1
            """,
            owner_id, name,
        )
        if existing:
            return {"ingredient": dict(existing), "created": False}

        row = await conn.fetchrow(
            """
            INSERT INTO ingredients (
                user_id, restaurant_id, branch_id, name, unit,
                current_stock, stock_quantity,
                reorder_point, reorder_quantity, reorder_level,
                minimum_stock, cost_per_unit, category, storage_location,
                storage_type, is_perishable, shelf_life_days, track_batches,
                sku, barcode, supplier, is_active, created_at, updated_at
            )
            VALUES (
                $1, $2::uuid, $3::uuid, $4, $5,
                $6, $6,
                $7, $8, COALESCE($7, 0),
                $9, $10, $11, $12,
                $13, $14, $15, $16,
                $17, $18, $19, TRUE, NOW(), NOW()
            )
            RETURNING *
            """,
            owner_id,
            user.restaurant_id,
            body.branch_id or user.branch_id,
            name, body.unit or "unit",
            float(body.current_stock or 0),
            body.reorder_point,
            body.reorder_quantity,
            body.minimum_stock,
            body.cost_per_unit,
            body.category,
            body.storage_location,
            body.storage_type or "dry",
            bool(body.is_perishable),
            body.shelf_life_days,
            bool(body.track_batches),
            body.sku, body.barcode, body.supplier,
        )

    ing = dict(row)

    if body.current_stock and float(body.current_stock) > 0 and user.restaurant_id:
        try:
            from app.services.inventory_event_service import inventory_event_service
            from app.core.events import INVENTORY_PURCHASED

            await inventory_event_service.append_event(
                restaurant_id=user.restaurant_id,
                branch_id=body.branch_id or user.branch_id,
                ingredient_id=str(ing["id"]),
                event_type=INVENTORY_PURCHASED,
                quantity_in=float(body.current_stock),
                quantity_out=0,
                unit_cost=float(body.cost_per_unit or 0),
                reference_type="ingredient_create",
                reference_id=str(ing["id"]),
                dedup_key=f"opening:{ing['id']}",
                source="manual",
                notes="Opening stock on ingredient create",
                created_by=user.user_id,
                ledger_type="opening",
                mirror_master=False,
            )
        except Exception as e:
            logger.warning(
                "ingredient_opening_event_failed",
                error=str(e), ingredient_id=ing["id"],
            )

    return {"ingredient": ing, "created": True}


@router.patch("/{ingredient_id}")
async def update_ingredient(
    ingredient_id: str,
    body: IngredientPatch,
    user: UserContext = Depends(require_permission("menu.write")),
):
    """Patch an ingredient master record. Stock changes go through
    `/inventory/adjustments` — this endpoint never mutates qty."""
    owner_id = _owner_id(user)
    fields = body.model_dump(exclude_unset=True)

    if not fields:
        raise HTTPException(400, "no fields to update")

    sets = []
    params: list = [owner_id, ingredient_id]
    idx = 3
    for k, v in fields.items():
        if k == "branch_id":
            sets.append(f"branch_id = ${idx}::uuid")
        else:
            sets.append(f"{k} = ${idx}")
        params.append(v)
        idx += 1
    sets.append("updated_at = NOW()")

    async with get_connection() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE ingredients SET {", ".join(sets)}
             WHERE user_id = $1 AND id = $2 AND deleted_at IS NULL
             RETURNING *
            """,
            *params,
        )
    if not row:
        raise HTTPException(404, "ingredient not found")
    return {"ingredient": dict(row)}


@router.delete("/{ingredient_id}", status_code=200)
async def delete_ingredient(
    ingredient_id: str,
    user: UserContext = Depends(require_permission("menu.delete")),
):
    """Soft-delete an ingredient. Ledger history is preserved."""
    owner_id = _owner_id(user)
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE ingredients
               SET deleted_at = NOW(), is_active = FALSE, updated_at = NOW()
             WHERE user_id = $1 AND id = $2 AND deleted_at IS NULL
             RETURNING id
            """,
            owner_id, ingredient_id,
        )
    if not row:
        raise HTTPException(404, "ingredient not found")
    return {"deleted": True, "id": ingredient_id}
