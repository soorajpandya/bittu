"""
Google Business Profile — Performance Insights Service.

Fetches views, calls, direction requests, and other metrics.
Uses the Business Profile Performance API.
Caches via Redis, persists daily snapshots to DB.
"""
from datetime import date, timedelta

from app.core.database import get_connection
from app.core.logging import get_logger
from app.core.exceptions import NotFoundError
from app.core.events import DomainEvent, emit_and_publish
from app.services.google.token_manager import GoogleTokenManager
from app.services.google.api_client import google_api, PERFORMANCE_BASE, _cache_key

logger = get_logger(__name__)

token_mgr = GoogleTokenManager()

INSIGHTS_CACHE_TTL = 600  # 10 minutes


class GoogleInsightsService:
    """Fetch Google Business Profile performance metrics."""

    async def get_performance_metrics(
        self,
        user_id: str,
        restaurant_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict:
        """
        Fetch daily performance metrics (views, searches, calls, direction requests).
        Defaults to last 30 days if dates not provided.
        """
        conn_row = await token_mgr.get_connection_for_restaurant(user_id, restaurant_id)
        if not conn_row or not conn_row.get("account_id") or not conn_row.get("location_id"):
            raise NotFoundError("Google location", "Connect and select a location first.")

        location_id = conn_row["location_id"]

        if not end_date:
            end_date = date.today() - timedelta(days=1)
        if not start_date:
            start_date = end_date - timedelta(days=30)

        location_resource = f"locations/{location_id}"

        metrics_data = await self._fetch_daily_metrics(
            user_id, restaurant_id, location_resource, start_date, end_date
        )

        # Persist to DB for historical queries
        await self._persist_metrics_db(restaurant_id, metrics_data)

        return {
            "location_id": location_id,
            "location_name": conn_row.get("location_name", ""),
            "period": {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
            },
            "metrics": metrics_data,
        }

    async def get_metrics_from_db(
        self,
        restaurant_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict:
        """Read metrics directly from DB (offline-capable)."""
        if not end_date:
            end_date = date.today() - timedelta(days=1)
        if not start_date:
            start_date = end_date - timedelta(days=30)

        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT metric_date, metric_name, metric_value
                FROM google_insights_daily
                WHERE restaurant_id = $1
                  AND metric_date BETWEEN $2 AND $3
                ORDER BY metric_date, metric_name
                """,
                restaurant_id,
                start_date,
                end_date,
            )

        # Group by metric name
        result: dict[str, list[dict]] = {}
        for row in rows:
            name = row["metric_name"]
            if name not in result:
                result[name] = []
            result[name].append({
                "date": row["metric_date"].isoformat(),
                "value": row["metric_value"],
            })

        return {
            "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "metrics": result,
        }

    async def _fetch_daily_metrics(
        self,
        user_id: str,
        restaurant_id: str,
        location_resource: str,
        start_date: date,
        end_date: date,
    ) -> dict:
        """Fetch daily metrics time series from the Performance API."""
        url = (
            f"{PERFORMANCE_BASE}/{location_resource}"
            ":fetchMultiDailyMetricsTimeSeries"
        )

        params = {
            "dailyMetrics": [
                "BUSINESS_IMPRESSIONS_DESKTOP_MAPS",
                "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH",
                "BUSINESS_IMPRESSIONS_MOBILE_MAPS",
                "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
                "CALL_CLICKS",
                "WEBSITE_CLICKS",
                "BUSINESS_DIRECTION_REQUESTS",
                "BUSINESS_BOOKINGS",
            ],
            "dailyRange.startDate.year": start_date.year,
            "dailyRange.startDate.month": start_date.month,
            "dailyRange.startDate.day": start_date.day,
            "dailyRange.endDate.year": end_date.year,
            "dailyRange.endDate.month": end_date.month,
            "dailyRange.endDate.day": end_date.day,
        }

        cache_extra = f"{start_date.isoformat()}:{end_date.isoformat()}"
        data = await google_api.request(
            "GET",
            url,
            user_id,
            restaurant_id,
            params=params,
            cache_key=_cache_key("insights", restaurant_id, cache_extra),
            cache_ttl=INSIGHTS_CACHE_TTL,
        )

        time_series = data.get("multiDailyMetricTimeSeries", [])

        result = {}
        for series in time_series:
            metric_name = series.get("dailyMetric", "UNKNOWN")
            data_points = []
            for ts in series.get("timeSeries", {}).get("datedValues", []):
                dt = ts.get("date", {})
                data_points.append({
                    "date": f"{dt.get('year', 0)}-{dt.get('month', 0):02d}-{dt.get('day', 0):02d}",
                    "value": int(ts.get("value", 0)),
                })
            result[metric_name] = data_points

        return result

    async def get_summary(
        self,
        user_id: str,
        restaurant_id: str,
        days: int = 30,
    ) -> dict:
        """Aggregated summary of key metrics for dashboard card."""
        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=days)

        full = await self.get_performance_metrics(
            user_id, restaurant_id, start_date, end_date
        )
        metrics = full.get("metrics", {})

        def _sum_metric(key: str) -> int:
            return sum(p.get("value", 0) for p in metrics.get(key, []))

        total_impressions = (
            _sum_metric("BUSINESS_IMPRESSIONS_DESKTOP_MAPS")
            + _sum_metric("BUSINESS_IMPRESSIONS_DESKTOP_SEARCH")
            + _sum_metric("BUSINESS_IMPRESSIONS_MOBILE_MAPS")
            + _sum_metric("BUSINESS_IMPRESSIONS_MOBILE_SEARCH")
        )

        return {
            **full,
            "summary": {
                "total_impressions": total_impressions,
                "total_calls": _sum_metric("CALL_CLICKS"),
                "total_website_clicks": _sum_metric("WEBSITE_CLICKS"),
                "total_direction_requests": _sum_metric("BUSINESS_DIRECTION_REQUESTS"),
                "total_bookings": _sum_metric("BUSINESS_BOOKINGS"),
                "period_days": days,
            },
        }

    async def sync_insights(self, user_id: str, restaurant_id: str, days: int = 90) -> int:
        """
        Full sync of insights to DB. Called by background sync.
        Returns count of data points persisted.
        """
        conn_row = await token_mgr.get_connection_for_restaurant(user_id, restaurant_id)
        if not conn_row or not conn_row.get("location_id"):
            return 0

        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=days)
        location_resource = f"locations/{conn_row['location_id']}"

        try:
            metrics = await self._fetch_daily_metrics(
                user_id, restaurant_id, location_resource, start_date, end_date
            )
        except Exception as e:
            logger.error("google_insights_sync_failed", error=str(e))
            return 0

        count = await self._persist_metrics_db(restaurant_id, metrics)
        await token_mgr.update_sync_timestamp(user_id, restaurant_id, "insights")
        logger.info("google_insights_synced", restaurant_id=restaurant_id, count=count)
        return count

    # ── Private ──────────────────────────────────────────────

    async def _persist_metrics_db(self, restaurant_id: str, metrics: dict) -> int:
        """Upsert daily metric rows to google_insights_daily."""
        total = 0
        async with get_connection() as conn:
            for metric_name, data_points in metrics.items():
                for dp in data_points:
                    try:
                        metric_date = date.fromisoformat(dp["date"])
                    except (ValueError, KeyError):
                        continue

                    await conn.execute(
                        """
                        INSERT INTO google_insights_daily
                            (restaurant_id, metric_date, metric_name, metric_value)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (restaurant_id, metric_date, metric_name) DO UPDATE SET
                            metric_value = EXCLUDED.metric_value
                        """,
                        restaurant_id,
                        metric_date,
                        metric_name,
                        dp.get("value", 0),
                    )
                    total += 1
        return total
