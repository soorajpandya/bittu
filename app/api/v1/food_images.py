"""
Food Image Pipeline API endpoints.

POST /food-images/generate   — batch generate (max 50)
POST /food-images             — bulk lookup by normalized names
GET  /food-images/{name}      — single lookup by normalized name
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.services.food_image_service import FoodImageService, MAX_BATCH_SIZE

router = APIRouter(prefix="/food-images", tags=["Food Images"])
_svc = FoodImageService()


# ── Request / Response models ──

class GenerateRequest(BaseModel):
    food_names: list[str] = Field(..., max_length=MAX_BATCH_SIZE)


class BulkLookupRequest(BaseModel):
    names: list[str]


# ── Endpoints ──

@router.post("/generate")
async def generate_food_images(
    body: GenerateRequest,
    user: UserContext = Depends(require_permission("menu.write")),
):
    """
    Batch generate food images. Cached items return instantly.
    Keys in results = raw food name (NOT normalized).
    Max 50 items per request, 5 minute timeout.
    """
    if len(body.food_names) > MAX_BATCH_SIZE:
        raise HTTPException(400, f"Max {MAX_BATCH_SIZE} items per batch")
    if not body.food_names:
        return {"results": {}}

    results = await _svc.generate_batch(body.food_names)
    return {"results": results}


@router.post("")
async def bulk_lookup_food_images(
    body: BulkLookupRequest,
    user: UserContext = Depends(require_permission("menu.read")),
):
    """
    Bulk lookup existing food images by normalized names.
    Missing names are silently omitted.
    """
    if not body.names:
        return {"results": {}}

    results = await _svc.bulk_lookup(body.names)
    return {"results": results}


@router.get("/{name}")
async def get_food_image(
    name: str,
    user: UserContext = Depends(require_permission("menu.read")),
):
    """
    Single lookup by normalized name.
    Returns 404 if not found.
    """
    result = await _svc.single_lookup(name)
    if not result:
        raise HTTPException(404, f"Food image not found: {name}")
    return result
