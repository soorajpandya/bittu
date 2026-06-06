"""Items CRUD endpoints."""
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Any, Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, ConfigDict
import asyncpg
import structlog
import orjson

from app.core.auth import UserContext, get_current_user
from app.core.database import get_connection
from app.core.tenant import tenant_where_clause
from app.core.exceptions import ForbiddenError
from app.core.redis import cache_get, cache_set, cache_delete_pattern

_ITEMS_CACHE_TTL = 300  # 5 minutes

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/items", tags=["Menu"])


# ── Pricing helpers ──────────────────────────────────────────

_ALLOWED_GST_RATES = {0, 5, 12, 18, 28}
_TWO = Decimal("0.01")


def _q2(value: float | int | Decimal | None) -> Optional[Decimal]:
    """Round to 2 decimal places, half-up."""
    if value is None:
        return None
    return Decimal(str(value)).quantize(_TWO, rounding=ROUND_HALF_UP)


def _resolve_pricing(
    *,
    pricing_type: Optional[str],
    gst_rate: Optional[float],
    is_tax_inclusive: Optional[bool],
    price_before_tax: Optional[float],
    final_price: Optional[float],
    price: Optional[float],
    existing: Optional[dict] = None,
) -> dict:
    """Validate + normalise the pricing/GST quintet.

    Rules (server-enforced, never trust the client for `final_price`):
      * pricing_type ∈ {mrp, configurable}
      * gst_rate ∈ {0,5,12,18,28}
      * mrp           → is_tax_inclusive=true,  final_price = price (= MRP)
                        price_before_tax derived from final_price / (1+gst%)
      * configurable  → is_tax_inclusive=false, price_before_tax required > 0
                        final_price = price_before_tax × (1 + gst_rate/100)
      * legacy `price` mirrors:
            mrp          → price = final_price
            configurable → price = price_before_tax
    Returns a dict of canonical column values to persist.
    """
    ex = existing or {}

    pt = (pricing_type or ex.get("pricing_type") or "configurable").lower()
    if pt not in ("mrp", "configurable"):
        raise HTTPException(422, "pricing_type must be 'mrp' or 'configurable'")

    rate = gst_rate if gst_rate is not None else ex.get("gst_rate")
    if rate is None:
        rate = 5
    try:
        rate_f = float(rate)
    except (TypeError, ValueError):
        raise HTTPException(422, "gst_rate must be numeric")
    if rate_f not in _ALLOWED_GST_RATES:
        raise HTTPException(422, f"gst_rate must be one of {sorted(_ALLOWED_GST_RATES)}")

    if pt == "mrp":
        # MRP-mode: final_price (or legacy price) = printed MRP, GST baked in.
        mrp = final_price if final_price is not None else (price if price is not None else ex.get("final_price"))
        if mrp is None:
            raise HTTPException(422, "mrp items require `final_price` (or legacy `price`)")
        mrp_d = _q2(mrp)
        base_d = _q2(Decimal(str(mrp_d)) / (Decimal("1") + Decimal(str(rate_f)) / Decimal("100")))
        return {
            "pricing_type":     "mrp",
            "gst_rate":         rate_f,
            "is_tax_inclusive": True,
            "price_before_tax": base_d,
            "final_price":      mrp_d,
            "price":            mrp_d,        # legacy mirror
        }

    # configurable
    base = price_before_tax
    if base is None:
        # fall back to legacy `price` on first save / patch when omitted
        base = price if price is not None else ex.get("price_before_tax")
    if base is None or float(base) <= 0:
        raise HTTPException(422, "configurable items require `price_before_tax` > 0")
    base_d = _q2(base)
    final_d = _q2(Decimal(str(base_d)) * (Decimal("1") + Decimal(str(rate_f)) / Decimal("100")))
    return {
        "pricing_type":     "configurable",
        "gst_rate":         rate_f,
        "is_tax_inclusive": False,
        "price_before_tax": base_d,
        "final_price":      final_d,
        "price":            base_d,           # legacy mirror = pre-tax base
    }


def require_owner_or_manager(user: UserContext = Depends(get_current_user)) -> UserContext:
    """Require owner or manager role."""
    if user.role not in ("owner", "manager"):
        raise ForbiddenError(f"Role '{user.role}' is not allowed. Required: owner or manager")
    return user


# ── Schemas ──────────────────────────────────────────────────

class ItemCreate(BaseModel):
    Item_Name: str
    Description: Optional[str] = None
    price: Optional[float] = None
    # Pricing / GST taxonomy (see migration 058)
    pricing_type: Literal["mrp", "configurable"] = "configurable"
    gst_rate: float = 5
    is_tax_inclusive: Optional[bool] = None
    price_before_tax: Optional[float] = None
    final_price: Optional[float] = None
    Available_Status: bool = True
    Category: Optional[str] = None
    Subcategory: Optional[str] = None
    Cuisine: Optional[str] = None
    Spice_Level: Optional[str] = None
    Prep_Time_Min: Optional[int] = None
    Image_url: Optional[str] = None
    is_veg: Optional[bool] = None
    tags: Optional[List[str]] = None
    sort_order: Optional[int] = None
    dine_in_available: bool = True
    takeaway_available: bool = True
    delivery_available: bool = True


class ItemUpdate(BaseModel):
    Item_Name: Optional[str] = None
    Description: Optional[str] = None
    price: Optional[float] = None
    pricing_type: Optional[Literal["mrp", "configurable"]] = None
    gst_rate: Optional[float] = None
    is_tax_inclusive: Optional[bool] = None
    price_before_tax: Optional[float] = None
    final_price: Optional[float] = None
    Available_Status: Optional[bool] = None
    Category: Optional[str] = None
    Subcategory: Optional[str] = None
    Cuisine: Optional[str] = None
    Spice_Level: Optional[str] = None
    Prep_Time_Min: Optional[int] = None
    Image_url: Optional[str] = None
    is_veg: Optional[bool] = None
    tags: Optional[List[str]] = None
    sort_order: Optional[int] = None
    dine_in_available: Optional[bool] = None
    takeaway_available: Optional[bool] = None
    delivery_available: Optional[bool] = None


class ItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    Item_ID: int
    Item_Name: str
    Description: Optional[str] = None
    price: Any  # numeric → Decimal from DB
    pricing_type: Optional[str] = None
    gst_rate: Optional[Any] = None
    is_tax_inclusive: Optional[bool] = None
    price_before_tax: Optional[Any] = None
    final_price: Optional[Any] = None
    Available_Status: Optional[bool] = None
    Category: Optional[str] = None
    Subcategory: Optional[str] = None
    Cuisine: Optional[str] = None
    Spice_Level: Optional[str] = None
    Prep_Time_Min: Optional[int] = None
    Image_url: Optional[str] = None
    is_veg: Optional[bool] = None
    tags: Optional[List[str]] = None
    sort_order: Optional[int] = None
    dine_in_available: Optional[bool] = None
    takeaway_available: Optional[bool] = None
    delivery_available: Optional[bool] = None
    restaurant_id: Optional[UUID] = None
    branch_id: Optional[UUID] = None
    user_id: Optional[UUID] = None
    created_at: Optional[Any] = None
    updated_at: Optional[Any] = None


# ── Routes ───────────────────────────────────────────────────

@router.get("", response_model=List[ItemResponse])
async def list_items(
    user: UserContext = Depends(require_owner_or_manager),
    branch_id: Optional[str] = Query(None),
    restaurant_id: Optional[str] = Query(None),
):
    clause, params = tenant_where_clause(user, "i")

    conditions = [clause]
    if branch_id:
        params.append(branch_id)
        conditions.append(f"i.branch_id = ${len(params)}")
    if restaurant_id:
        params.append(restaurant_id)
        conditions.append(f"i.restaurant_id = ${len(params)}")

    where = " AND ".join(conditions)

    # Only cache the unfiltered full list (most common call)
    uid = user.owner_id if user.is_branch_user else user.user_id
    cache_key = f"items_list:{uid}" if not branch_id and not restaurant_id else None
    if cache_key:
        try:
            cached = await cache_get(cache_key)
            if cached:
                return orjson.loads(cached)
        except Exception:
            pass

    async with get_connection() as conn:
        items = await conn.fetch(
            f"SELECT * FROM items i WHERE {where} ORDER BY i.created_at DESC",
            *params,
        )
        result = [dict(item) for item in items]

    if cache_key:
        try:
            await cache_set(cache_key, orjson.dumps(result, default=str).decode(), ttl=_ITEMS_CACHE_TTL)
        except Exception:
            pass
    return result


@router.post("", response_model=ItemResponse)
async def create_item(
    body: ItemCreate,
    user: UserContext = Depends(require_owner_or_manager),
):
    # ── Resolve pricing/GST quintet (server is the source of truth) ──
    pricing = _resolve_pricing(
        pricing_type=body.pricing_type,
        gst_rate=body.gst_rate,
        is_tax_inclusive=body.is_tax_inclusive,
        price_before_tax=body.price_before_tax,
        final_price=body.final_price,
        price=body.price,
    )
    legacy_price = pricing["price"]

    # ── Duplicate check: same name + price for this tenant ──
    clause, check_params = tenant_where_clause(user)
    check_params.extend([body.Item_Name.strip(), legacy_price])
    async with get_connection() as conn:
        existing = await conn.fetchrow(
            f"""SELECT "Item_ID", "Item_Name", price FROM items
                WHERE {clause}
                AND LOWER(TRIM("Item_Name")) = LOWER(TRIM(${len(check_params) - 1}))
                AND price = ${len(check_params)}""",
            *check_params,
        )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Item '{body.Item_Name}' with price {legacy_price} already exists (ID: {existing['Item_ID']})",
        )

    fields = [
        '"Item_Name"', '"Description"', 'price', '"Available_Status"', '"Category"',
        '"Subcategory"', '"Cuisine"', '"Spice_Level"', '"Prep_Time_Min"', '"Image_url"',
        'is_veg', 'tags', 'sort_order', 'dine_in_available', 'takeaway_available',
        'delivery_available', 'restaurant_id', 'branch_id', 'user_id',
        'pricing_type', 'gst_rate', 'is_tax_inclusive', 'price_before_tax', 'final_price',
    ]
    values = [
        body.Item_Name, body.Description, legacy_price, body.Available_Status, body.Category,
        body.Subcategory, body.Cuisine, body.Spice_Level, body.Prep_Time_Min, body.Image_url,
        body.is_veg, body.tags, body.sort_order, body.dine_in_available, body.takeaway_available,
        body.delivery_available, user.restaurant_id, user.branch_id, user.user_id,
        pricing["pricing_type"], pricing["gst_rate"], pricing["is_tax_inclusive"],
        pricing["price_before_tax"], pricing["final_price"],
    ]
    placeholders = ", ".join(f"${i+1}" for i in range(len(values)))

    sql = f"INSERT INTO items ({', '.join(fields)}) VALUES ({placeholders}) RETURNING *"
    async with get_connection() as conn:
        item = await conn.fetchrow(sql, *values)
    uid = user.owner_id if user.is_branch_user else user.user_id
    try:
        await cache_delete_pattern(f"items_list:{uid}*")
        await cache_delete_pattern(f"menu_all:{uid}*")
    except Exception:
        pass
    return dict(item)


# ── Bulk Import (AI Menu Scan) ───────────────────────────────

class BulkImportItem(BaseModel):
    item_name: str
    description: Optional[str] = None
    price: float
    category: Optional[str] = None
    subcategory: Optional[str] = None
    cuisine: Optional[str] = None
    spice_level: Optional[str] = None
    prep_time_min: Optional[int] = None
    image_url: Optional[str] = None
    is_veg: Optional[bool] = None
    short_code: Optional[str] = None


class BulkImportRequest(BaseModel):
    items: List[BulkImportItem]
    skip_duplicates: bool = True  # default: skip dupes silently


class BulkImportResult(BaseModel):
    total_submitted: int
    created: int
    skipped_duplicates: int
    created_items: List[dict]
    skipped_items: List[dict]


@router.post("/bulk-import", response_model=BulkImportResult)
async def bulk_import_items(
    body: BulkImportRequest,
    user: UserContext = Depends(require_owner_or_manager),
):
    """
    Bulk import items (used by AI Menu Scan confirm flow).
    Skips duplicates by matching name (case-insensitive) + price per tenant.
    """
    if not body.items:
        return BulkImportResult(
            total_submitted=0, created=0, skipped_duplicates=0,
            created_items=[], skipped_items=[],
        )

    if len(body.items) > 200:
        raise HTTPException(400, "Max 200 items per bulk import")

    clause, base_params = tenant_where_clause(user)
    created_items = []
    skipped_items = []

    async with get_connection() as conn:
        # Fetch ALL existing item names+prices for this tenant in one query
        existing_rows = await conn.fetch(
            f"""SELECT LOWER(TRIM("Item_Name")) as norm_name, price
                FROM items WHERE {clause}""",
            *base_params,
        )
        # Build a set of (normalized_name, price) for O(1) lookup
        existing_set = {
            (row["norm_name"], float(row["price"]))
            for row in existing_rows
        }

        for item in body.items:
            name_norm = item.item_name.strip().lower()
            key = (name_norm, float(item.price))

            if key in existing_set:
                skipped_items.append({
                    "item_name": item.item_name,
                    "price": item.price,
                    "reason": "duplicate",
                })
                continue

            # AI scan defaults: configurable @ 5% GST; treat incoming price as
            # pre-tax base so final_price stays consistent with the rest of API.
            pricing = _resolve_pricing(
                pricing_type="configurable",
                gst_rate=5,
                is_tax_inclusive=False,
                price_before_tax=item.price,
                final_price=None,
                price=item.price,
            )
            row = await conn.fetchrow(
                """INSERT INTO items (
                    "Item_Name", "Description", price, "Available_Status",
                    "Category", "Subcategory", "Cuisine", "Spice_Level",
                    "Prep_Time_Min", "Image_url", is_veg,
                    restaurant_id, branch_id, user_id,
                    pricing_type, gst_rate, is_tax_inclusive,
                    price_before_tax, final_price
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,
                          $15,$16,$17,$18,$19)
                RETURNING *""",
                item.item_name, item.description, pricing["price"], True,
                item.category, item.subcategory, item.cuisine, item.spice_level,
                item.prep_time_min, item.image_url, item.is_veg,
                user.restaurant_id, user.branch_id, user.user_id,
                pricing["pricing_type"], pricing["gst_rate"], pricing["is_tax_inclusive"],
                pricing["price_before_tax"], pricing["final_price"],
            )
            created_items.append(dict(row))
            # Add to set so subsequent dupes within same batch are caught
            existing_set.add(key)

    logger.info(
        "bulk_import_complete",
        total=len(body.items),
        created=len(created_items),
        skipped=len(skipped_items),
    )

    if created_items:
        uid = user.owner_id if user.is_branch_user else user.user_id
        try:
            await cache_delete_pattern(f"items_list:{uid}*")
            await cache_delete_pattern(f"menu_all:{uid}*")
        except Exception:
            pass

    return BulkImportResult(
        total_submitted=len(body.items),
        created=len(created_items),
        skipped_duplicates=len(skipped_items),
        created_items=created_items,
        skipped_items=skipped_items,
    )


@router.get("/{item_id}", response_model=ItemResponse)
async def get_item(
    item_id: int,
    user: UserContext = Depends(require_owner_or_manager),
):
    clause, params = tenant_where_clause(user, "i")
    params.append(item_id)

    async with get_connection() as conn:
        item = await conn.fetchrow(
            f'SELECT * FROM items i WHERE {clause} AND i."Item_ID" = ${len(params)}',
            *params,
        )
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")
        return dict(item)


@router.put("/{item_id}", response_model=ItemResponse)
@router.patch("/{item_id}", response_model=ItemResponse)
async def update_item(
    item_id: int,
    body: ItemUpdate,
    user: UserContext = Depends(require_owner_or_manager),
):
    # Map Pydantic field names to quoted DB column names
    _col_map = {
        "Item_Name": '"Item_Name"', "Description": '"Description"', "price": "price",
        "Available_Status": '"Available_Status"', "Category": '"Category"',
        "Subcategory": '"Subcategory"', "Cuisine": '"Cuisine"',
        "Spice_Level": '"Spice_Level"', "Prep_Time_Min": '"Prep_Time_Min"',
        "Image_url": '"Image_url"', "is_veg": "is_veg", "tags": "tags",
        "sort_order": "sort_order", "dine_in_available": "dine_in_available",
        "takeaway_available": "takeaway_available", "delivery_available": "delivery_available",
        "pricing_type": "pricing_type", "gst_rate": "gst_rate",
        "is_tax_inclusive": "is_tax_inclusive", "price_before_tax": "price_before_tax",
        "final_price": "final_price",
    }
    patch = body.model_dump(exclude_unset=True)

    # If any pricing field was touched, re-resolve the whole quintet from
    # current row + patch so `final_price` is always derived server-side.
    pricing_fields = {"pricing_type", "gst_rate", "is_tax_inclusive",
                      "price_before_tax", "final_price", "price"}
    if patch.keys() & pricing_fields:
        async with get_connection() as conn:
            existing = await conn.fetchrow(
                'SELECT pricing_type, gst_rate, is_tax_inclusive, '
                'price_before_tax, final_price, price '
                'FROM items WHERE "Item_ID" = $1',
                item_id,
            )
        ex_dict = dict(existing) if existing else {}
        pricing = _resolve_pricing(
            pricing_type=patch.get("pricing_type"),
            gst_rate=patch.get("gst_rate"),
            is_tax_inclusive=patch.get("is_tax_inclusive"),
            price_before_tax=patch.get("price_before_tax"),
            final_price=patch.get("final_price"),
            price=patch.get("price"),
            existing={k: float(v) if isinstance(v, Decimal) else v
                      for k, v in ex_dict.items() if v is not None},
        )
        # Replace any client-supplied pricing with the canonical resolved set.
        for k in pricing_fields:
            patch.pop(k, None)
        patch.update(pricing)

    updates = []
    params = []
    for field, value in patch.items():
        if value is None:
            continue
        col = _col_map.get(field, field)
        updates.append(f"{col} = ${len(params)+1}")
        params.append(value)

    if not updates:
        return await get_item(item_id, user)

    set_clause = ", ".join(updates)

    # Build tenant WHERE with correct param offset (after SET params)
    offset = len(params)
    if user.is_branch_user:
        params.extend([user.owner_id, user.branch_id])
        where_clause = f"user_id = ${offset+1} AND branch_id = ${offset+2}"
    else:
        params.append(user.user_id)
        where_clause = f"user_id = ${offset+1}"
    params.append(item_id)

    async with get_connection() as conn:
        item = await conn.fetchrow(
            f'UPDATE items SET {set_clause} WHERE {where_clause} AND "Item_ID" = ${len(params)} RETURNING *',
            *params,
        )
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")
    uid = user.owner_id if user.is_branch_user else user.user_id
    try:
        await cache_delete_pattern(f"items_list:{uid}*")
        await cache_delete_pattern(f"menu_all:{uid}*")
    except Exception:
        pass
    return dict(item)


@router.delete("/{item_id}")
async def delete_item(
    item_id: int,
    user: UserContext = Depends(require_owner_or_manager),
):
    clause, params = tenant_where_clause(user)
    params.append(item_id)

    async with get_connection() as conn:
        try:
            result = await conn.execute(
                f'DELETE FROM items WHERE {clause} AND "Item_ID" = ${len(params)}',
                *params,
            )
        except asyncpg.exceptions.RaiseError as exc:
            # A DB guard trigger blocked the cascade (e.g. gst_invoice_items
            # immutability). Surface a 409 with the trigger's message instead
            # of a 500.
            raise HTTPException(status_code=409, detail=str(exc))
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="Item not found")
    uid = user.owner_id if user.is_branch_user else user.user_id
    try:
        await cache_delete_pattern(f"items_list:{uid}*")
        await cache_delete_pattern(f"menu_all:{uid}*")
    except Exception:
        pass
    return {"message": "Item deleted"}


# ── Nested sub-resource routes ───────────────────────────────
from app.services.item_customization_service import ItemVariantService, ItemAddonService, ItemExtraService
from app.services.item_station_service import ItemStationService
from app.services.modifier_service import ModifierService

_variant_svc = ItemVariantService()
_addon_svc = ItemAddonService()
_extra_svc = ItemExtraService()
_station_svc = ItemStationService()
_modifier_svc = ModifierService()


@router.get("/{item_id}/variants")
async def list_item_variants(
    item_id: int,
    user: UserContext = Depends(require_owner_or_manager),
):
    return await _variant_svc.list_variants(user, item_id=item_id)


@router.get("/{item_id}/addons")
async def list_item_addons(
    item_id: int,
    user: UserContext = Depends(require_owner_or_manager),
):
    return await _addon_svc.list_addons(user, item_id=item_id)


@router.get("/{item_id}/extras")
async def list_item_extras(
    item_id: int,
    user: UserContext = Depends(require_owner_or_manager),
):
    return await _extra_svc.list_extras(user, item_id=item_id)


@router.get("/{item_id}/station-mappings")
async def list_item_station_mappings(
    item_id: int,
    user: UserContext = Depends(require_owner_or_manager),
):
    return await _station_svc.list_mappings(user, item_id=item_id)


@router.get("/{item_id}/modifier-groups")
async def list_item_modifier_groups(
    item_id: int,
    user: UserContext = Depends(require_owner_or_manager),
):
    return await _modifier_svc.list_groups(user)