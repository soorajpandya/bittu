"""
Google Business Profile — Posts Service.

Create promotional/update posts on a Google Business location.
"""
import httpx

from app.core.logging import get_logger
from app.core.exceptions import AppException, NotFoundError
from app.services.google.token_manager import GoogleTokenManager

logger = get_logger(__name__)

MY_BUSINESS_BASE = "https://mybusiness.googleapis.com/v4"

token_mgr = GoogleTokenManager()


class GooglePostsService:
    """Create and manage Google Business local posts."""

    async def create_post(
        self,
        user_id: str,
        restaurant_id: str,
        summary: str,
        action_type: str | None = None,
        action_url: str | None = None,
        image_url: str | None = None,
        event: dict | None = None,
        offer: dict | None = None,
    ) -> dict:
        """
        Create a local post on the connected Google Business location.

        Post types supported:
          - STANDARD: Just text (+ optional image)
          - EVENT: Has event title + schedule
          - OFFER: Has coupon code, terms, etc.

        Args:
            summary: Post body text (1500 chars max).
            action_type: One of BOOK, ORDER, SHOP, SIGN_UP, LEARN_MORE, CALL.
            action_url: URL for the CTA button.
            image_url: Public URL of the image to attach.
            event: {"title": str, "schedule": {"startDate": {...}, "endDate": {...}}}
            offer: {"couponCode": str, "termsConditions": str, ...}
        """
        conn = await token_mgr.get_connection_for_restaurant(user_id, restaurant_id)
        if not conn or not conn.get("account_id") or not conn.get("location_id"):
            raise NotFoundError("Google location", "Connect and select a location first.")

        access_token = await token_mgr.get_valid_token(user_id, restaurant_id)
        account_id = conn["account_id"]
        location_id = conn["location_id"]

        # Build post payload
        post_body: dict = {
            "languageCode": "en",
            "summary": summary[:1500],
            "topicType": "STANDARD",
        }

        if event:
            post_body["topicType"] = "EVENT"
            post_body["event"] = event

        if offer:
            post_body["topicType"] = "OFFER"
            post_body["offer"] = offer

        if action_type and action_url:
            post_body["callToAction"] = {
                "actionType": action_type.upper(),
                "url": action_url,
            }

        if image_url:
            post_body["media"] = [
                {"mediaFormat": "PHOTO", "sourceUrl": image_url}
            ]

        url = (
            f"{MY_BUSINESS_BASE}/accounts/{account_id}"
            f"/locations/{location_id}/localPosts"
        )

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                url,
                json=post_body,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )

        if resp.status_code not in (200, 201):
            logger.error("google_create_post_failed", status=resp.status_code, body=resp.text)
            raise AppException(
                status_code=resp.status_code,
                detail=f"Failed to create post: {resp.text}",
                error_code="GOOGLE_API_ERROR",
            )

        logger.info(
            "google_post_created",
            user_id=user_id,
            restaurant_id=restaurant_id,
        )
        return resp.json()

    async def list_posts(
        self,
        user_id: str,
        restaurant_id: str,
        page_size: int = 20,
        page_token: str | None = None,
    ) -> dict:
        """List existing local posts for the connected location."""
        conn = await token_mgr.get_connection_for_restaurant(user_id, restaurant_id)
        if not conn or not conn.get("account_id") or not conn.get("location_id"):
            raise NotFoundError("Google location", "Connect and select a location first.")

        access_token = await token_mgr.get_valid_token(user_id, restaurant_id)
        account_id = conn["account_id"]
        location_id = conn["location_id"]

        params: dict = {"pageSize": min(page_size, 100)}
        if page_token:
            params["pageToken"] = page_token

        url = (
            f"{MY_BUSINESS_BASE}/accounts/{account_id}"
            f"/locations/{location_id}/localPosts"
        )

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )

        if resp.status_code != 200:
            logger.error("google_list_posts_failed", status=resp.status_code, body=resp.text)
            raise AppException(
                status_code=resp.status_code,
                detail=f"Failed to list posts: {resp.text}",
                error_code="GOOGLE_API_ERROR",
            )

        data = resp.json()
        return {
            "posts": data.get("localPosts", []),
            "next_page_token": data.get("nextPageToken"),
        }
