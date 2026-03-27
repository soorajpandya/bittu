"""
Google Business Profile — Posts Service.

Create, list, and manage promotional/update posts on a Google Business location.
Validates URLs, persists to DB, emits events, caches reads.
"""
from urllib.parse import urlparse

from app.core.database import get_connection
from app.core.logging import get_logger
from app.core.exceptions import NotFoundError, ValidationError
from app.core.events import DomainEvent, emit_and_publish
from app.services.google.token_manager import GoogleTokenManager
from app.services.google.api_client import google_api, MY_BUSINESS_BASE, _cache_key

logger = get_logger(__name__)

token_mgr = GoogleTokenManager()

POSTS_CACHE_TTL = 120  # 2 minutes

VALID_ACTION_TYPES = {"BOOK", "ORDER", "SHOP", "SIGN_UP", "LEARN_MORE", "CALL"}


def _validate_url(url: str, label: str) -> str:
    """Validate that a URL is well-formed HTTPS."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError(f"Invalid {label}: must be a valid HTTP(S) URL")
    return url


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

        Post types: STANDARD, EVENT, OFFER.
        """
        conn_row = await token_mgr.get_connection_for_restaurant(user_id, restaurant_id)
        if not conn_row or not conn_row.get("account_id") or not conn_row.get("location_id"):
            raise NotFoundError("Google location", "Connect and select a location first.")

        # ── Input validation ──
        if not summary or not summary.strip():
            raise ValidationError("Post summary is required")
        summary = summary.strip()[:1500]

        if action_type:
            action_type = action_type.upper()
            if action_type not in VALID_ACTION_TYPES:
                raise ValidationError(
                    f"Invalid action_type. Must be one of: {', '.join(sorted(VALID_ACTION_TYPES))}"
                )

        if action_url:
            _validate_url(action_url, "action_url")
        if image_url:
            _validate_url(image_url, "image_url")

        account_id = conn_row["account_id"]
        location_id = conn_row["location_id"]

        # ── Build post payload ──
        post_body: dict = {
            "languageCode": "en",
            "summary": summary,
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
                "actionType": action_type,
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

        result = await google_api.request(
            "POST",
            url,
            user_id,
            restaurant_id,
            json_body=post_body,
        )

        # ── Persist to DB ──
        await self._upsert_post_db(restaurant_id, result)

        # ── Invalidate cache ──
        await google_api.invalidate_cache("posts", restaurant_id)

        # ── Emit event ──
        await emit_and_publish(DomainEvent(
            event_type="google.post_created",
            payload={
                "topic_type": post_body["topicType"],
                "summary": summary[:100],
            },
            user_id=user_id,
            restaurant_id=restaurant_id,
        ))

        logger.info(
            "google_post_created",
            user_id=user_id,
            restaurant_id=restaurant_id,
            topic_type=post_body["topicType"],
        )
        return result

    async def list_posts(
        self,
        user_id: str,
        restaurant_id: str,
        page_size: int = 20,
        page_token: str | None = None,
    ) -> dict:
        """List existing local posts for the connected location."""
        conn_row = await token_mgr.get_connection_for_restaurant(user_id, restaurant_id)
        if not conn_row or not conn_row.get("account_id") or not conn_row.get("location_id"):
            raise NotFoundError("Google location", "Connect and select a location first.")

        account_id = conn_row["account_id"]
        location_id = conn_row["location_id"]

        params: dict = {"pageSize": min(page_size, 100)}
        if page_token:
            params["pageToken"] = page_token

        cache_extra = f"{page_size}:{page_token or ''}"
        data = await google_api.request(
            "GET",
            f"{MY_BUSINESS_BASE}/accounts/{account_id}/locations/{location_id}/localPosts",
            user_id,
            restaurant_id,
            params=params,
            cache_key=_cache_key("posts", restaurant_id, cache_extra),
            cache_ttl=POSTS_CACHE_TTL,
        )

        posts = data.get("localPosts", [])

        # Persist to DB in background
        if posts:
            await self._upsert_posts_db(restaurant_id, posts)

        return {
            "posts": posts,
            "next_page_token": data.get("nextPageToken"),
        }

    async def sync_posts(self, user_id: str, restaurant_id: str) -> int:
        """Full sync of posts from Google API to DB. Returns count."""
        conn_row = await token_mgr.get_connection_for_restaurant(user_id, restaurant_id)
        if not conn_row or not conn_row.get("account_id") or not conn_row.get("location_id"):
            return 0

        account_id = conn_row["account_id"]
        location_id = conn_row["location_id"]
        total = 0
        page_token = None

        while True:
            params: dict = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token

            try:
                data = await google_api.request(
                    "GET",
                    f"{MY_BUSINESS_BASE}/accounts/{account_id}/locations/{location_id}/localPosts",
                    user_id,
                    restaurant_id,
                    params=params,
                )
            except Exception as e:
                logger.error("google_post_sync_page_failed", error=str(e))
                break

            posts = data.get("localPosts", [])
            if posts:
                await self._upsert_posts_db(restaurant_id, posts)
                total += len(posts)

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        await token_mgr.update_sync_timestamp(user_id, restaurant_id, "posts")
        logger.info("google_posts_synced", restaurant_id=restaurant_id, count=total)
        return total

    # ── Private ──────────────────────────────────────────────

    async def _upsert_post_db(self, restaurant_id: str, post: dict) -> None:
        """Persist a single post to DB."""
        await self._upsert_posts_db(restaurant_id, [post])

    async def _upsert_posts_db(self, restaurant_id: str, posts: list[dict]) -> None:
        """Persist a list of posts to DB."""
        async with get_connection() as conn:
            for p in posts:
                name = p.get("name", "")
                # Extract post_id from resource name (last segment)
                post_id = name.rsplit("/", 1)[-1] if name else ""
                if not post_id:
                    continue

                await conn.execute(
                    """
                    INSERT INTO google_posts
                        (restaurant_id, post_id, topic_type, summary, state, raw_data, synced_at)
                    VALUES ($1, $2, $3, $4, $5, $6, now())
                    ON CONFLICT (restaurant_id, post_id) DO UPDATE SET
                        topic_type = EXCLUDED.topic_type,
                        summary    = EXCLUDED.summary,
                        state      = EXCLUDED.state,
                        raw_data   = EXCLUDED.raw_data,
                        synced_at  = now()
                    """,
                    restaurant_id,
                    post_id,
                    p.get("topicType", "STANDARD"),
                    p.get("summary", ""),
                    p.get("state", ""),
                    p,
                )
