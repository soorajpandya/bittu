"""AI Ingredient endpoints — suggest and auto-link ingredients for menu items."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_role
from app.services.ai_ingredient_service import AIIngredientService

router = APIRouter(prefix="/ai-ingredients", tags=["AI Ingredients"])
_svc = AIIngredientService()


class SuggestRequest(BaseModel):
    item_name: str


class AutoLinkRequest(BaseModel):
    item_id: int
    item_name: str


@router.post("/suggest")
async def suggest_ingredients(
    body: SuggestRequest,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    """AI-powered ingredient suggestions for a dish name (does not save)."""
    return await _svc.suggest_ingredients(body.item_name)


@router.post("/auto-link", status_code=201)
async def auto_link_ingredients(
    body: AutoLinkRequest,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    """AI-suggest ingredients, match/create raw materials, link to item."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await _svc.auto_link_ingredients(uid, body.item_id, body.item_name)
