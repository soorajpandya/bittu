"""Restaurant Settings endpoints."""
from typing import Optional, Any
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.cache import cached_route, invalidate_prefix
from app.services.restaurant_settings_service import RestaurantSettingsService
from app.services.tax_engine import (
    TaxConfigError, validate_gst_settings_patch, invalidate_tax_config,
)

router = APIRouter(prefix="/restaurant-settings", tags=["Restaurant Settings"])
_svc = RestaurantSettingsService()
_CACHE_PREFIX = "restaurant_settings"


class SettingsUpdate(BaseModel):
    # Legacy single-rate field — kept for back-compat; new clients should
    # use gst_percentage + cgst/sgst split below.
    tax_percentage: Optional[float] = Field(None, ge=0, le=28)
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
    # GST configuration (M065)
    gst_enabled: Optional[bool] = None
    gst_type: Optional[str] = Field(None, max_length=20)
    gst_number: Optional[str] = Field(None, max_length=20)
    gst_percentage: Optional[float] = Field(None, ge=0, le=28)
    cgst_percentage: Optional[float] = Field(None, ge=0, le=28)
    sgst_percentage: Optional[float] = Field(None, ge=0, le=28)
    tax_inclusive: Optional[bool] = None


@router.get("")
@cached_route(prefix=_CACHE_PREFIX, ttl=300)
async def get_settings(
    user: UserContext = Depends(require_permission("settings.read")),
):
    return await _svc.get_settings(user)


@router.put("")
async def update_settings(
    body: SettingsUpdate,
    user: UserContext = Depends(require_permission("settings.admin")),
):
    payload = body.model_dump(exclude_unset=True)

    # Validate the GST-related subset against the currently-stored row so
    # callers don't have to resend gst_number on every PUT.
    gst_keys = {
        "gst_enabled", "gst_type", "gst_number",
        "gst_percentage", "cgst_percentage", "sgst_percentage",
        "tax_inclusive",
    }
    if any(k in payload for k in gst_keys):
        existing = await _svc.get_settings(user)
        try:
            cleaned = validate_gst_settings_patch(
                {k: payload[k] for k in payload if k in gst_keys},
                existing_gst_number=(existing or {}).get("gst_number"),
            )
        except TaxConfigError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc),
            )
        # Merge validated/derived values back into the payload.
        for k, v in cleaned.items():
            payload[k] = v
        # Keep the legacy tax_percentage column in sync with gst_percentage
        # so any read path that hasn't been migrated still gets the right rate.
        if "gst_percentage" in payload and "tax_percentage" not in payload:
            payload["tax_percentage"] = payload["gst_percentage"]

    result = await _svc.upsert_settings(user, payload)
    await invalidate_prefix(_CACHE_PREFIX, user)
    invalidate_tax_config(user.restaurant_id)
    return result
