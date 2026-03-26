"""
Google Business Profile — Reviews Service.

Fetches reviews and posts replies.
"""
import httpx

from app.core.logging import get_logger
from app.core.exceptions import AppException, NotFoundError
from app.services.google.token_manager import GoogleTokenManager

logger = get_logger(__name__)

MY_BUSINESS_BASE = "https://mybusiness.googleapis.com/v4"

token_mgr = GoogleTokenManager()


class GoogleReviewsService:
    """Read and reply to Google Business reviews."""

    async def list_reviews(
        self,
        user_id: str,
        restaurant_id: str,
        page_size: int = 50,
        page_token: str | None = None,
    ) -> dict:
        """
        Fetch reviews for the connected location.
        Returns reviews + pagination info.
        """
        conn = await token_mgr.get_connection_for_restaurant(user_id, restaurant_id)
        if not conn or not conn.get("account_id") or not conn.get("location_id"):
            raise NotFoundError("Google location", "Connect and select a location first.")

        access_token = await token_mgr.get_valid_token(user_id, restaurant_id)
        account_id = conn["account_id"]
        location_id = conn["location_id"]

        params: dict = {"pageSize": min(page_size, 50)}
        if page_token:
            params["pageToken"] = page_token

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{MY_BUSINESS_BASE}/accounts/{account_id}/locations/{location_id}/reviews",
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )

        if resp.status_code != 200:
            logger.error("google_list_reviews_failed", status=resp.status_code, body=resp.text)
            raise AppException(
                status_code=resp.status_code,
                detail=f"Failed to fetch reviews: {resp.text}",
                error_code="GOOGLE_API_ERROR",
            )

        data = resp.json()
        return {
            "reviews": data.get("reviews", []),
            "average_rating": data.get("averageRating"),
            "total_review_count": data.get("totalReviewCount"),
            "next_page_token": data.get("nextPageToken"),
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
        """
        conn = await token_mgr.get_connection_for_restaurant(user_id, restaurant_id)
        if not conn or not conn.get("account_id") or not conn.get("location_id"):
            raise NotFoundError("Google location", "Connect and select a location first.")

        access_token = await token_mgr.get_valid_token(user_id, restaurant_id)
        account_id = conn["account_id"]
        location_id = conn["location_id"]

        url = (
            f"{MY_BUSINESS_BASE}/accounts/{account_id}"
            f"/locations/{location_id}/reviews/{review_id}/reply"
        )

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.put(
                url,
                json={"comment": reply_text},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )

        if resp.status_code not in (200, 201):
            logger.error("google_reply_review_failed", status=resp.status_code, body=resp.text)
            raise AppException(
                status_code=resp.status_code,
                detail=f"Failed to reply to review: {resp.text}",
                error_code="GOOGLE_API_ERROR",
            )

        logger.info(
            "google_review_replied",
            user_id=user_id,
            restaurant_id=restaurant_id,
            review_id=review_id,
        )
        return resp.json()
