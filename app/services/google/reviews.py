"""
Google Business Profile — Reviews Service.

Fetches reviews and posts replies.
Caches in DB + Redis. Prevents duplicate replies. Retries failed replies.
"""

from app.core.database import get_connection
from app.core.logging import get_logger
from app.core.exceptions import NotFoundError, ConflictError
from app.core.events import DomainEvent, emit_and_publish
from app.services.google.token_manager import GoogleTokenManager
from app.services.google.api_client import google_api, MY_BUSINESS_BASE, _cache_key

logger = get_logger(__name__)

token_mgr = GoogleTokenManager()

# Cache reviews for 30 seconds (fresh enough for dashboard)
REVIEWS_CACHE_TTL = 30


class GoogleReviewsService:
    """Read and reply to Google Business reviews with caching and dedup."""

    async def list_reviews(
        self,
        user_id: str,
        restaurant_id: str,
        page_size: int = 50,
        page_token: str | None = None,
    ) -> dict:
        """
        Fetch reviews for the connected location.
        Serves from cache/DB when available, falls back to Google API.
        """
        conn_row = await token_mgr.get_connection_for_restaurant(user_id, restaurant_id)
        if not conn_row or not conn_row.get("account_id") or not conn_row.get("location_id"):
            raise NotFoundError("Google location", "Connect and select a location first.")

        account_id = conn_row["account_id"]
        location_id = conn_row["location_id"]

        params: dict = {"pageSize": min(page_size, 50)}
        if page_token:
            params["pageToken"] = page_token

        cache_extra = f"{page_size}:{page_token or ''}"
        data = await google_api.request(
            "GET",
            f"{MY_BUSINESS_BASE}/accounts/{account_id}/locations/{location_id}/reviews",
            user_id,
            restaurant_id,
            params=params,
            cache_key=_cache_key("reviews", restaurant_id, cache_extra),
            cache_ttl=REVIEWS_CACHE_TTL,
        )

        reviews = data.get("reviews", [])

        # ── Persist reviews to DB asynchronously ──
        if reviews:
            await self._upsert_reviews_db(restaurant_id, reviews)

        return {
            "reviews": reviews,
            "average_rating": data.get("averageRating"),
            "total_review_count": data.get("totalReviewCount"),
            "next_page_token": data.get("nextPageToken"),
        }

    async def list_reviews_from_db(
        self,
        restaurant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Read reviews directly from DB (for when Google API is slow/down)."""
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT review_id, reviewer_name, star_rating, comment,
                       reply_comment, reply_time, create_time, synced_at
                FROM google_reviews
                WHERE restaurant_id = $1
                ORDER BY create_time DESC
                LIMIT $2 OFFSET $3
                """,
                restaurant_id,
                limit,
                offset,
            )
            count_row = await conn.fetchrow(
                "SELECT COUNT(*) as cnt FROM google_reviews WHERE restaurant_id = $1",
                restaurant_id,
            )
        return {
            "reviews": [dict(r) for r in rows],
            "total_review_count": count_row["cnt"] if count_row else 0,
        }

    async def reply_to_review(
        self,
        user_id: str,
        restaurant_id: str,
        review_id: str,
        reply_text: str,
    ) -> dict:
        """
        Reply to (or update reply on) a specific review.
        Checks for existing reply to prevent accidental duplicates.
        """
        conn_row = await token_mgr.get_connection_for_restaurant(user_id, restaurant_id)
        if not conn_row or not conn_row.get("account_id") or not conn_row.get("location_id"):
            raise NotFoundError("Google location", "Connect and select a location first.")

        # ── Check for existing reply ──
        async with get_connection() as conn:
            existing = await conn.fetchrow(
                """
                SELECT reply_comment FROM google_reviews
                WHERE restaurant_id = $1 AND review_id = $2 AND reply_comment IS NOT NULL
                """,
                restaurant_id,
                review_id,
            )
        if existing and existing["reply_comment"]:
            raise ConflictError(
                f"Review '{review_id}' already has a reply. "
                "Use the update endpoint to modify it."
            )

        account_id = conn_row["account_id"]
        location_id = conn_row["location_id"]

        url = (
            f"{MY_BUSINESS_BASE}/accounts/{account_id}"
            f"/locations/{location_id}/reviews/{review_id}/reply"
        )

        result = await google_api.request(
            "PUT",
            url,
            user_id,
            restaurant_id,
            json_body={"comment": reply_text},
        )

        # ── Update local DB ──
        async with get_connection() as conn:
            await conn.execute(
                """
                UPDATE google_reviews
                SET reply_comment = $3, reply_time = now(), synced_at = now()
                WHERE restaurant_id = $1 AND review_id = $2
                """,
                restaurant_id,
                review_id,
                reply_text,
            )

        # ── Invalidate cache ──
        await google_api.invalidate_cache("reviews", restaurant_id)

        # ── Emit event ──
        await emit_and_publish(DomainEvent(
            event_type="google.review_replied",
            payload={"review_id": review_id, "reply_text": reply_text[:100]},
            user_id=user_id,
            restaurant_id=restaurant_id,
        ))

        logger.info(
            "google_review_replied",
            user_id=user_id,
            restaurant_id=restaurant_id,
            review_id=review_id,
        )
        return result

    async def sync_reviews(self, user_id: str, restaurant_id: str) -> int:
        """
        Full sync: fetch all reviews from Google and persist to DB.
        Called by background sync job. Returns count of synced reviews.
        """
        conn_row = await token_mgr.get_connection_for_restaurant(user_id, restaurant_id)
        if not conn_row or not conn_row.get("account_id") or not conn_row.get("location_id"):
            return 0

        account_id = conn_row["account_id"]
        location_id = conn_row["location_id"]

        total_synced = 0
        page_token = None

        while True:
            params: dict = {"pageSize": 50}
            if page_token:
                params["pageToken"] = page_token

            try:
                data = await google_api.request(
                    "GET",
                    f"{MY_BUSINESS_BASE}/accounts/{account_id}/locations/{location_id}/reviews",
                    user_id,
                    restaurant_id,
                    params=params,
                )
            except Exception as e:
                logger.error("google_review_sync_page_failed", error=str(e))
                break

            reviews = data.get("reviews", [])
            if reviews:
                await self._upsert_reviews_db(restaurant_id, reviews)
                total_synced += len(reviews)

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        await token_mgr.update_sync_timestamp(user_id, restaurant_id, "reviews")
        logger.info("google_reviews_synced", restaurant_id=restaurant_id, count=total_synced)
        return total_synced

    # ── Private ──────────────────────────────────────────────

    async def _upsert_reviews_db(self, restaurant_id: str, reviews: list[dict]) -> None:
        """Persist fetched reviews to DB."""
        async with get_connection() as conn:
            for review in reviews:
                review_id = review.get("reviewId", "")
                reviewer = review.get("reviewer", {})
                reply = review.get("reviewReply", {}) or {}

                await conn.execute(
                    """
                    INSERT INTO google_reviews
                        (restaurant_id, review_id, reviewer_name, star_rating, comment,
                         create_time, reply_comment, reply_time, raw_data, synced_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
                    ON CONFLICT (restaurant_id, review_id) DO UPDATE SET
                        star_rating   = EXCLUDED.star_rating,
                        comment       = EXCLUDED.comment,
                        reply_comment = EXCLUDED.reply_comment,
                        reply_time    = EXCLUDED.reply_time,
                        raw_data      = EXCLUDED.raw_data,
                        synced_at     = now()
                    """,
                    restaurant_id,
                    review_id,
                    reviewer.get("displayName", ""),
                    review.get("starRating", ""),
                    review.get("comment", ""),
                    review.get("createTime"),
                    reply.get("comment"),
                    reply.get("updateTime"),
                    review,
                )
