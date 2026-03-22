"""Restaurant Settings Service — GET/PUT for restaurant_settings."""
from typing import Optional
import json
from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class RestaurantSettingsService:

    async def get_settings(self, user: UserContext) -> dict:
        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM restaurant_settings WHERE user_id = $1",
                user.owner_id if user.is_branch_user else user.user_id,
            )
        if not row:
            return {}
        return dict(row)

    async def upsert_settings(self, user: UserContext, data: dict) -> dict:
        uid = user.owner_id if user.is_branch_user else user.user_id
        # Convert dict fields to JSON strings for jsonb columns
        for json_field in ("printer_config", "theme_config"):
            if json_field in data and isinstance(data[json_field], dict):
                data[json_field] = json.dumps(data[json_field])

        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                "SELECT id FROM restaurant_settings WHERE user_id = $1 FOR UPDATE",
                uid,
            )
            if existing:
                fields = {k: v for k, v in data.items() if v is not None}
                if not fields:
                    row = await conn.fetchrow(
                        "SELECT * FROM restaurant_settings WHERE user_id = $1", uid
                    )
                    return dict(row)
                set_parts = []
                vals = [uid]
                for k, v in fields.items():
                    vals.append(v)
                    set_parts.append(f"{k} = ${len(vals)}")
                row = await conn.fetchrow(
                    f"UPDATE restaurant_settings SET {', '.join(set_parts)} WHERE user_id = $1 RETURNING *",
                    *vals,
                )
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO restaurant_settings (
                        user_id, restaurant_id, tax_percentage, currency,
                        receipt_header, receipt_footer, auto_accept_orders,
                        enable_qr_ordering, enable_delivery, enable_dine_in, enable_takeaway,
                        printer_config, theme_config,
                        enable_led_display, led_display_url, enable_dual_screen, dual_screen_url
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
                    RETURNING *
                    """,
                    uid,
                    user.restaurant_id,
                    data.get("tax_percentage", 0),
                    data.get("currency", "INR"),
                    data.get("receipt_header"),
                    data.get("receipt_footer"),
                    data.get("auto_accept_orders", False),
                    data.get("enable_qr_ordering", False),
                    data.get("enable_delivery", False),
                    data.get("enable_dine_in", True),
                    data.get("enable_takeaway", True),
                    data.get("printer_config"),
                    data.get("theme_config"),
                    data.get("enable_led_display", False),
                    data.get("led_display_url"),
                    data.get("enable_dual_screen", False),
                    data.get("dual_screen_url"),
                )
        logger.info("restaurant_settings_saved", user_id=uid)
        return dict(row)
