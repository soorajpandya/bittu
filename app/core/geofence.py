"""
Geo-fence enforcement helper.

Used by `/orders/checkout` and `/payments/initiate` to ensure the customer's
device GPS is within the branch's configured radius (default 100 m). Skipped
entirely when:
  - the branch has no lat/lng configured, or
  - `geofence_enabled = FALSE` on the branch row, or
  - the caller did not supply customer coordinates (NULL pair).

Distance is computed in Postgres via `fn_haversine_meters` (migration 063)
so we never round-trip the branch coordinates back to Python.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.core.database import get_connection
from app.core.exceptions import ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)


async def assert_within_geofence(
    *,
    merchant_id: str,
    branch_id: Optional[str],
    customer_lat: Optional[float | Decimal],
    customer_lng: Optional[float | Decimal],
) -> None:
    """
    Raise ValidationError if (customer_lat, customer_lng) is farther than the
    branch's configured `geofence_radius_meters`. No-op when the branch has
    not opted into geo-fencing or when either coordinate is missing.
    """
    if customer_lat is None or customer_lng is None:
        return  # caller did not provide GPS — fall back to legacy behaviour
    if not branch_id:
        return  # no branch row to anchor against (single-location merchants)

    try:
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT b.latitude,
                       b.longitude,
                       b.geofence_radius_meters,
                       b.geofence_enabled,
                       fn_haversine_meters(
                           b.latitude, b.longitude,
                           $3::numeric, $4::numeric
                       ) AS distance_m
                FROM sub_branches b
                WHERE b.id = $1::uuid AND b.restaurant_id = $2::uuid
                """,
                str(branch_id), str(merchant_id),
                float(customer_lat), float(customer_lng),
            )
    except Exception:
        # Never fail-open silently for crashes — log and skip enforcement
        # rather than blocking checkout when DB is flaky. Geofence is a
        # secondary control; payment integrity comes from the rest of the
        # pipeline.
        logger.exception(
            "geofence_lookup_failed",
            merchant_id=str(merchant_id),
            branch_id=str(branch_id),
        )
        return

    if row is None:
        return  # unknown branch_id — let downstream validation catch it
    if not row["geofence_enabled"]:
        return
    if row["latitude"] is None or row["longitude"] is None:
        return

    radius_m = int(row["geofence_radius_meters"] or 100)
    distance_m = row["distance_m"]
    if distance_m is None:
        return  # NULL ⇒ haversine couldn't compute (shouldn't happen here)

    if distance_m > radius_m:
        logger.info(
            "geofence_block",
            merchant_id=str(merchant_id),
            branch_id=str(branch_id),
            distance_m=float(distance_m),
            radius_m=radius_m,
        )
        raise ValidationError(
            f"Outside branch geofence: you are {int(distance_m)} m away "
            f"(allowed {radius_m} m). Please pay at the counter."
        )
