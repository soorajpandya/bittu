"""Menu — bulk fetch all catalog data in one request.

Replaces the 11 individual startup calls the Flutter admin app makes with
a single request that uses one DB connection and one Redis cache key.
"""
import orjson
from fastapi import APIRouter, Depends

from app.core.auth import UserContext, require_permission
from app.core.database import get_connection
from app.core.redis import cache_get, cache_set, cache_delete_pattern

router = APIRouter(prefix="/menu", tags=["Menu"])

_MENU_CACHE_TTL = 300  # 5 minutes


def _menu_cache_key(uid: str) -> str:
    return f"menu_all:{uid}"


@router.get("/all")
async def get_full_menu(
    user: UserContext = Depends(require_permission("menu.read")),
):
    """Return all catalog data in one request.

    Replaces individual calls to /items, /categories, /combos,
    /item-variants, /item-addons, /item-extras, /modifier-groups,
    /modifiers/options, and /item-stations.

    Cached per owner for 5 minutes. Call POST /menu/cache/invalidate
    after any menu write to clear it immediately.
    """
    uid = user.owner_id if user.is_branch_user else user.user_id
    branch_id = user.branch_id if user.is_branch_user else None
    cache_key = _menu_cache_key(uid)

    try:
        cached = await cache_get(cache_key)
        if cached:
            return orjson.loads(cached)
    except Exception:
        pass

    async with get_connection() as conn:
        # All queries share one connection — avoids acquiring 8+ separate connections.
        if branch_id:
            items = await conn.fetch(
                "SELECT * FROM items WHERE user_id = $1 AND (branch_id = $2 OR branch_id IS NULL) ORDER BY created_at DESC",
                uid, branch_id,
            )
            categories = await conn.fetch(
                "SELECT * FROM categories WHERE user_id = $1 ORDER BY sort_order, name",
                uid,
            )
            combos = await conn.fetch(
                "SELECT * FROM combos WHERE user_id = $1 ORDER BY name",
                uid,
            )
            variants = await conn.fetch(
                "SELECT * FROM item_variants WHERE user_id = $1 ORDER BY item_id, name",
                uid,
            )
            addons = await conn.fetch(
                "SELECT * FROM item_addons WHERE user_id = $1 ORDER BY item_id, name",
                uid,
            )
            extras = await conn.fetch(
                "SELECT * FROM item_extras WHERE user_id = $1 ORDER BY item_id, name",
                uid,
            )
        else:
            items = await conn.fetch(
                "SELECT * FROM items WHERE user_id = $1 ORDER BY created_at DESC",
                uid,
            )
            categories = await conn.fetch(
                "SELECT * FROM categories WHERE user_id = $1 ORDER BY sort_order, name",
                uid,
            )
            combos = await conn.fetch(
                "SELECT * FROM combos WHERE user_id = $1 ORDER BY name",
                uid,
            )
            variants = await conn.fetch(
                "SELECT * FROM item_variants WHERE user_id = $1 ORDER BY item_id, name",
                uid,
            )
            addons = await conn.fetch(
                "SELECT * FROM item_addons WHERE user_id = $1 ORDER BY item_id, name",
                uid,
            )
            extras = await conn.fetch(
                "SELECT * FROM item_extras WHERE user_id = $1 ORDER BY item_id, name",
                uid,
            )

        modifier_groups = await conn.fetch(
            "SELECT * FROM modifier_groups WHERE user_id = $1 ORDER BY name",
            uid,
        )
        modifier_options = await conn.fetch(
            """
            SELECT mo.*, mg.name AS group_name
            FROM modifier_options mo
            JOIN modifier_groups mg ON mg.id = mo.group_id
            WHERE mg.user_id = $1
            ORDER BY mg.name, mo.name
            """,
            uid,
        )
        item_stations = await conn.fetch(
            "SELECT * FROM item_station_mapping WHERE user_id = $1 ORDER BY item_id",
            uid,
        )

    result = {
        "items": [dict(r) for r in items],
        "categories": [dict(r) for r in categories],
        "combos": [dict(r) for r in combos],
        "item_variants": [dict(r) for r in variants],
        "item_addons": [dict(r) for r in addons],
        "item_extras": [dict(r) for r in extras],
        "modifier_groups": [dict(r) for r in modifier_groups],
        "modifier_options": [dict(r) for r in modifier_options],
        "item_stations": [dict(r) for r in item_stations],
    }

    try:
        await cache_set(cache_key, orjson.dumps(result, default=str).decode(), ttl=_MENU_CACHE_TTL)
    except Exception:
        pass

    return result


@router.post("/cache/invalidate")
async def invalidate_menu_cache(
    user: UserContext = Depends(require_permission("menu.write")),
):
    """Manually invalidate the /menu/all cache for this restaurant.

    Call this after bulk menu edits to force fresh data on next request.
    The cache auto-expires after 5 minutes regardless.
    """
    uid = user.owner_id if user.is_branch_user else user.user_id
    try:
        await cache_delete_pattern(f"menu_all:{uid}*")
    except Exception:
        pass
    return {"invalidated": True}
