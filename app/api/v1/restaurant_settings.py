"""Restaurant Settings endpoints."""
from typing import Optional, Any
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.restaurant_settings_service import RestaurantSettingsService

router = APIRouter(prefix="/restaurant-settings", tags=["Restaurant Settings"])
_svc = RestaurantSettingsService()


class SettingsUpdate(BaseModel):
    tax_percentage: Optional[float] = None
    currency: Optional[str] = None
    receipt_header: Optional[str] = None
    receipt_footer: Optional[str] = None
    auto_accept_orders: Optional[bool] = None
    enable_qr_ordering: Optional[bool] = None
    enable_delivery: Optional[bool] = None
    enable_dine_in: Optional[bool] = None
    enable_takeaway: Optional[bool] = None
    printer_config: Optional[dict] = None
    theme_config: Optional[dict] = None
    enable_led_display: Optional[bool] = None
    led_display_url: Optional[str] = None
    enable_dual_screen: Optional[bool] = None
    dual_screen_url: Optional[str] = None


@router.get("")
async def get_settings(
    user: UserContext = Depends(require_permission("settings.read")),
):
    return await _svc.get_settings(user)


@router.put("")
async def update_settings(
    body: SettingsUpdate,
    user: UserContext = Depends(require_permission("settings.admin")),
):
    return await _svc.upsert_settings(user, body.model_dump(exclude_unset=True))
