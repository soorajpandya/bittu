"""
Google Business Profile — Performance Insights Service.

Fetches views, calls, direction requests, and other metrics.
Uses the Business Profile Performance API.
"""
import httpx
from datetime import date, timedelta

from app.core.logging import get_logger
from app.core.exceptions import AppException, NotFoundError
from app.services.google.token_manager import GoogleTokenManager

logger = get_logger(__name__)

PERFORMANCE_BASE = "https://businessprofileperformance.googleapis.com/v1"

token_mgr = GoogleTokenManager()


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
        Uses the searchkeywords:fetchMultiDailyMetricsTimeSeries endpoint.
        """
        conn = await token_mgr.get_connection_for_restaurant(user_id, restaurant_id)
        if not conn or not conn.get("account_id") or not conn.get("location_id"):
            raise NotFoundError("Google location", "Connect and select a location first.")

        access_token = await token_mgr.get_valid_token(user_id, restaurant_id)
        location_id = conn["location_id"]

        if not end_date:
            end_date = date.today() - timedelta(days=1)  # Yesterday (data lag)
        if not start_date:
            start_date = end_date - timedelta(days=30)

        location_resource = f"locations/{location_id}"

        # Fetch multi daily metrics
        metrics_data = await self._fetch_daily_metrics(
            access_token, location_resource, start_date, end_date
        )

        return {
            "location_id": location_id,
            "location_name": conn.get("location_name", ""),
            "period": {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
            },
            "metrics": metrics_data,
        }

    async def _fetch_daily_metrics(
        self,
        access_token: str,
        location_resource: str,
        start_date: date,
        end_date: date,
    ) -> dict:
        """
        Fetch daily metrics time series from the Performance API.
        """
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

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )

        if resp.status_code != 200:
            logger.error(
                "google_insights_fetch_failed",
                status=resp.status_code,
                body=resp.text,
            )
            raise AppException(
                status_code=resp.status_code,
                detail=f"Failed to fetch insights: {resp.text}",
                error_code="GOOGLE_API_ERROR",
            )

        data = resp.json()
        time_series = data.get("multiDailyMetricTimeSeries", [])

        # Transform into a cleaner structure
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
        """
        Convenience method: get an aggregated summary of key metrics.
        Suitable for a growth dashboard card.
        """
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
