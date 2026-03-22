"""
Analytics Pipeline Service.

Handles:
  - Daily analytics aggregation (orders, revenue, item popularity)
  - On-demand analytics queries
  - Dashboard data retrieval
  - Period comparisons
"""
from datetime import date, timedelta
from typing import Optional
from decimal import Decimal

from app.core.auth import UserContext
from app.core.database import get_connection, get_transaction
from app.core.redis import cache_get, cache_set
from app.core.tenant import tenant_where_clause
from app.core.logging import get_logger

logger = get_logger(__name__)


class AnalyticsService:

    # ------------------------------------------------------------------
    # Daily aggregation (called by scheduled job)
    # ------------------------------------------------------------------

    async def aggregate_daily(self, branch_id: str, target_date: date) -> dict:
        """Aggregate and upsert daily analytics for a branch + date."""
        async with get_transaction() as conn:
            stats = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)                                     AS total_orders,
                    COUNT(*) FILTER (WHERE status = 'completed') AS completed_orders,
                    COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled_orders,
                    COALESCE(SUM(total)  FILTER (WHERE status = 'completed'), 0) AS total_revenue,
                    COALESCE(SUM(tax)    FILTER (WHERE status = 'completed'), 0) AS total_tax,
                    COALESCE(SUM(discount) FILTER (WHERE status = 'completed'), 0) AS total_discount,
                    COALESCE(AVG(total)  FILTER (WHERE status = 'completed'), 0) AS avg_order_value,
                    -- dine-in / takeaway / delivery split
                    COUNT(*) FILTER (WHERE order_type = 'dine_in'  AND status = 'completed') AS dine_in_orders,
                    COUNT(*) FILTER (WHERE order_type = 'takeaway'  AND status = 'completed') AS takeaway_orders,
                    COUNT(*) FILTER (WHERE order_type = 'delivery'  AND status = 'completed') AS delivery_orders,
                    -- payment mode split
                    COUNT(*) FILTER (WHERE payment_mode = 'cash'   AND status = 'completed') AS cash_orders,
                    COUNT(*) FILTER (WHERE payment_mode = 'online' AND status = 'completed') AS online_orders
                FROM orders
                WHERE branch_id = $1 AND DATE(created_at) = $2
                """,
                branch_id, target_date,
            )

            top_items = await conn.fetch(
                """
                SELECT oi.item_id, mi.name, SUM(oi.quantity) AS qty, SUM(oi.price * oi.quantity) AS revenue
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                JOIN menu_items mi ON mi.id = oi.item_id
                WHERE o.branch_id = $1 AND DATE(o.created_at) = $2 AND o.status = 'completed'
                GROUP BY oi.item_id, mi.name
                ORDER BY qty DESC
                LIMIT 10
                """,
                branch_id, target_date,
            )

            import json

            top_items_json = json.dumps([dict(r) for r in top_items], default=str)

            await conn.execute(
                """
                INSERT INTO daily_analytics (
                    branch_id, date, total_orders, completed_orders, cancelled_orders,
                    total_revenue, total_tax, total_discount, avg_order_value,
                    dine_in_orders, takeaway_orders, delivery_orders,
                    cash_orders, online_orders, top_items
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::jsonb)
                ON CONFLICT (branch_id, date)
                DO UPDATE SET
                    total_orders     = EXCLUDED.total_orders,
                    completed_orders = EXCLUDED.completed_orders,
                    cancelled_orders = EXCLUDED.cancelled_orders,
                    total_revenue    = EXCLUDED.total_revenue,
                    total_tax        = EXCLUDED.total_tax,
                    total_discount   = EXCLUDED.total_discount,
                    avg_order_value  = EXCLUDED.avg_order_value,
                    dine_in_orders   = EXCLUDED.dine_in_orders,
                    takeaway_orders  = EXCLUDED.takeaway_orders,
                    delivery_orders  = EXCLUDED.delivery_orders,
                    cash_orders      = EXCLUDED.cash_orders,
                    online_orders    = EXCLUDED.online_orders,
                    top_items        = EXCLUDED.top_items,
                    updated_at       = NOW()
                """,
                branch_id, target_date,
                stats["total_orders"], stats["completed_orders"], stats["cancelled_orders"],
                stats["total_revenue"], stats["total_tax"], stats["total_discount"],
                stats["avg_order_value"],
                stats["dine_in_orders"], stats["takeaway_orders"], stats["delivery_orders"],
                stats["cash_orders"], stats["online_orders"],
                top_items_json,
            )

            logger.info("daily_analytics_aggregated", branch_id=branch_id, date=str(target_date))
            return dict(stats)

    # ------------------------------------------------------------------
    # Dashboard queries
    # ------------------------------------------------------------------

    async def get_dashboard(
        self,
        user: UserContext,
        branch_id: str,
        start_date: date,
        end_date: date,
    ) -> dict:
        """Return dashboard payload for a date range."""
        cache_key = f"analytics:dash:{branch_id}:{start_date}:{end_date}"
        cached = await cache_get(cache_key)
        if cached:
            return cached

        async with get_connection() as conn:
            summary = await conn.fetchrow(
                """
                SELECT
                    COALESCE(SUM(total_orders),     0) AS total_orders,
                    COALESCE(SUM(completed_orders),  0) AS completed_orders,
                    COALESCE(SUM(cancelled_orders),  0) AS cancelled_orders,
                    COALESCE(SUM(total_revenue),     0) AS total_revenue,
                    COALESCE(SUM(total_tax),         0) AS total_tax,
                    COALESCE(SUM(total_discount),    0) AS total_discount
                FROM daily_analytics
                WHERE branch_id = $1 AND date BETWEEN $2 AND $3
                """,
                branch_id, start_date, end_date,
            )

            daily = await conn.fetch(
                """
                SELECT date, total_orders, completed_orders, total_revenue
                FROM daily_analytics
                WHERE branch_id = $1 AND date BETWEEN $2 AND $3
                ORDER BY date
                """,
                branch_id, start_date, end_date,
            )

            top_items = await conn.fetch(
                """
                SELECT oi.item_id, mi.name,
                       SUM(oi.quantity) AS qty,
                       SUM(oi.price * oi.quantity) AS revenue
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                JOIN menu_items mi ON mi.id = oi.item_id
                WHERE o.branch_id = $1
                  AND DATE(o.created_at) BETWEEN $2 AND $3
                  AND o.status = 'completed'
                GROUP BY oi.item_id, mi.name
                ORDER BY qty DESC
                LIMIT 10
                """,
                branch_id, start_date, end_date,
            )

            payment_split = await conn.fetchrow(
                """
                SELECT
                    COALESCE(SUM(cash_orders),   0) AS cash,
                    COALESCE(SUM(online_orders), 0) AS online
                FROM daily_analytics
                WHERE branch_id = $1 AND date BETWEEN $2 AND $3
                """,
                branch_id, start_date, end_date,
            )

            order_type_split = await conn.fetchrow(
                """
                SELECT
                    COALESCE(SUM(dine_in_orders),  0) AS dine_in,
                    COALESCE(SUM(takeaway_orders), 0) AS takeaway,
                    COALESCE(SUM(delivery_orders), 0) AS delivery
                FROM daily_analytics
                WHERE branch_id = $1 AND date BETWEEN $2 AND $3
                """,
                branch_id, start_date, end_date,
            )

        result = {
            "summary": _row_to_serializable(summary),
            "daily": [_row_to_serializable(r) for r in daily],
            "top_items": [_row_to_serializable(r) for r in top_items],
            "payment_split": _row_to_serializable(payment_split),
            "order_type_split": _row_to_serializable(order_type_split),
        }

        await cache_set(cache_key, result, ttl=300)  # 5 min TTL
        return result

    async def compare_periods(
        self,
        branch_id: str,
        current_start: date,
        current_end: date,
        previous_start: date,
        previous_end: date,
    ) -> dict:
        """Compare two date ranges and return % change."""
        async with get_connection() as conn:
            async def _range_sum(s: date, e: date):
                return await conn.fetchrow(
                    """
                    SELECT COALESCE(SUM(total_revenue), 0) AS revenue,
                           COALESCE(SUM(completed_orders), 0) AS orders
                    FROM daily_analytics
                    WHERE branch_id = $1 AND date BETWEEN $2 AND $3
                    """,
                    branch_id, s, e,
                )

            current = await _range_sum(current_start, current_end)
            previous = await _range_sum(previous_start, previous_end)

            def _pct(cur, prev):
                if prev == 0:
                    return 100.0 if cur > 0 else 0.0
                return round(float((cur - prev) / prev * 100), 2)

            return {
                "current": {"revenue": float(current["revenue"]), "orders": int(current["orders"])},
                "previous": {"revenue": float(previous["revenue"]), "orders": int(previous["orders"])},
                "change": {
                    "revenue_pct": _pct(current["revenue"], previous["revenue"]),
                    "orders_pct": _pct(current["orders"], previous["orders"]),
                },
            }

    async def get_hourly_heatmap(self, branch_id: str, target_date: date) -> list[dict]:
        """Orders per hour for a given day — useful for staffing decisions."""
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT EXTRACT(HOUR FROM created_at)::int AS hour, COUNT(*) AS orders
                FROM orders
                WHERE branch_id = $1 AND DATE(created_at) = $2
                GROUP BY hour
                ORDER BY hour
                """,
                branch_id, target_date,
            )
            return [dict(r) for r in rows]


    async def track_funnel_event(
        self,
        user: UserContext,
        event: str,
        step: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        """Track a user funnel event (onboarding, feature adoption, etc.)."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        funnel_step = step or event or "unknown"
        try:
            async with get_connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO user_funnel_events (user_id, step, first_seen, last_seen, visit_count)
                    VALUES ($1, $2, $3, $3, 1)
                    ON CONFLICT (user_id) DO UPDATE
                    SET step = $2, last_seen = $3, visit_count = user_funnel_events.visit_count + 1
                    """,
                    user.user_id, funnel_step, now,
                )
        except Exception:
            logger.warning("funnel_event_tracking_failed", step=funnel_step, user_id=user.user_id)
        return {"step": funnel_step, "status": "tracked"}


def _row_to_serializable(row) -> dict:
    """Convert asyncpg Record to JSON-safe dict."""
    if row is None:
        return {}
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, Decimal):
            d[k] = float(v)
        elif isinstance(v, date):
            d[k] = v.isoformat()
    return d
