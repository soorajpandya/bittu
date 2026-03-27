"""
Google Business Profile — Background Sync Service.

Periodically syncs locations, reviews, posts, and insights
for all active Google Business connections.

Usage:
    Called from a background task (e.g. asyncio.create_task on startup
    or a scheduled job via APScheduler / cron-triggered endpoint).
"""
import asyncio

from app.core.logging import get_logger
from app.core.redis import DistributedLock, LockError
from app.services.google.token_manager import GoogleTokenManager
from app.services.google.reviews import GoogleReviewsService
from app.services.google.posts import GooglePostsService
from app.services.google.insights import GoogleInsightsService
from app.services.google.locations import GoogleLocationsService

logger = get_logger(__name__)

token_mgr = GoogleTokenManager()
reviews_svc = GoogleReviewsService()
posts_svc = GooglePostsService()
insights_svc = GoogleInsightsService()
locations_svc = GoogleLocationsService()

# Minimum interval between full syncs (seconds)
SYNC_LOCK_TTL = 600  # 10 min — prevents overlapping runs


async def sync_all_connections() -> dict:
    """
    Run a full sync for every active Google Business connection.

    Acquires a distributed lock so concurrent deploys / cron triggers
    don't double-sync.

    Returns summary: {"total": N, "succeeded": M, "failed": F, "details": [...]}
    """
    try:
        async with DistributedLock("google:sync:all", timeout=SYNC_LOCK_TTL):
            connections = await token_mgr.get_all_active_connections()
            logger.info("google_sync_starting", total=len(connections))

            results = []
            succeeded = 0
            failed = 0

            for conn in connections:
                user_id = conn["user_id"]
                restaurant_id = conn["restaurant_id"]
                detail: dict = {
                    "restaurant_id": restaurant_id,
                    "locations": 0,
                    "reviews": 0,
                    "posts": 0,
                    "insights": 0,
                    "error": None,
                }

                try:
                    detail["locations"] = await locations_svc.fetch_and_store_locations(
                        user_id, restaurant_id
                    )
                    detail["reviews"] = await reviews_svc.sync_reviews(
                        user_id, restaurant_id
                    )
                    detail["posts"] = await posts_svc.sync_posts(
                        user_id, restaurant_id
                    )
                    detail["insights"] = await insights_svc.sync_insights(
                        user_id, restaurant_id
                    )
                    succeeded += 1
                except Exception as e:
                    detail["error"] = str(e)[:200]
                    failed += 1
                    logger.error(
                        "google_sync_connection_failed",
                        restaurant_id=restaurant_id,
                        error=str(e),
                    )

                results.append(detail)

                # Small pause to avoid hammering Google API
                await asyncio.sleep(1)

            summary = {
                "total": len(connections),
                "succeeded": succeeded,
                "failed": failed,
                "details": results,
            }
            logger.info("google_sync_completed", **{k: v for k, v in summary.items() if k != "details"})
            return summary
    except LockError:
        logger.info("google_sync_skipped_locked")
        return {"skipped": True, "reason": "Another sync is running"}


async def sync_single_restaurant(user_id: str, restaurant_id: str) -> dict:
    """
    Sync a single restaurant's Google data (on-demand).
    Useful when an admin triggers "Refresh" from the dashboard.
    """
    try:
        async with DistributedLock(f"google:sync:{restaurant_id}", timeout=120):
            result: dict = {"restaurant_id": restaurant_id}
            result["locations"] = await locations_svc.fetch_and_store_locations(user_id, restaurant_id)
            result["reviews"] = await reviews_svc.sync_reviews(user_id, restaurant_id)
            result["posts"] = await posts_svc.sync_posts(user_id, restaurant_id)
            result["insights"] = await insights_svc.sync_insights(user_id, restaurant_id)

            logger.info("google_sync_single_completed", **result)
            return result
    except LockError:
        return {"skipped": True, "reason": "Sync already in progress for this restaurant"}
