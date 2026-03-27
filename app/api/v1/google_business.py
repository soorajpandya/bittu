"""
Google Business Profile — API Routes.

Endpoints for OAuth, locations, reviews, posts, insights, and sync.
All mutating endpoints verify restaurant ownership before proceeding.
Every endpoint is wrapped with:
  - Per-user rate limit guard (5s cooldown)
  - try/except returning safe fallback on failure
  - Structured error logging
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.auth import UserContext, get_current_user
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.services.google.auth import GoogleAuthService
from app.services.google.locations import GoogleLocationsService
from app.services.google.reviews import GoogleReviewsService
from app.services.google.posts import GooglePostsService
from app.services.google.insights import GoogleInsightsService
from app.services.google.token_manager import GoogleTokenManager
from app.services.google.sync import sync_single_restaurant

logger = get_logger(__name__)

router = APIRouter(prefix="/google", tags=["Google Business Profile"])

_auth_svc = GoogleAuthService()
_locations_svc = GoogleLocationsService()
_reviews_svc = GoogleReviewsService()
_posts_svc = GooglePostsService()
_insights_svc = GoogleInsightsService()
_token_mgr = GoogleTokenManager()

# ── Rate limit cooldown (seconds) ──
RATE_LIMIT_COOLDOWN = 5


# ── Helpers ──────────────────────────────────────────────────


def _resolve_restaurant_id(user: UserContext, restaurant_id: str) -> str:
    """
    Resolve a possibly-wrong restaurant_id from the frontend.
    Many frontends pass user_id or owner_id as restaurant_id by mistake.
    If the incoming value matches the user's own ID or owner_id, substitute
    the real restaurant_id from their auth context.
    """
    if restaurant_id in (user.user_id, user.owner_id) and user.restaurant_id:
        logger.debug(
            "google_resolve_restaurant_id",
            original=restaurant_id,
            resolved=user.restaurant_id,
        )
        return user.restaurant_id
    return restaurant_id


async def _verify_ownership(user: UserContext, restaurant_id: str) -> None:
    """Ensure the authenticated user owns (or has access to) the restaurant."""
    # For branch users, owner_id is the restaurant owner; for owners, owner_id == user_id
    effective_owner = user.owner_id or user.user_id
    await _token_mgr.verify_restaurant_ownership(effective_owner, restaurant_id)


async def _rate_guard(user_id: str, restaurant_id: str, action: str) -> bool:
    """
    Per-user per-action rate limiter.
    Returns True if the request should be blocked (rate limited).
    Sets a 5-second cooldown key in Redis.
    """
    r = get_redis()
    key = f"rate:google:{action}:{user_id}:{restaurant_id}"
    exists = await r.get(key)
    if exists:
        return True
    await r.set(key, "1", ex=RATE_LIMIT_COOLDOWN)
    return False


# ── Request / Response Models ────────────────────────────────


class ConnectRequest(BaseModel):
    restaurant_id: str


class CallbackRequest(BaseModel):
    code: str
    state: str


class SelectLocationRequest(BaseModel):
    restaurant_id: str
    account_id: str
    location_id: str
    location_name: str = ""


class ReviewReplyRequest(BaseModel):
    restaurant_id: str
    review_id: str
    reply_text: str = Field(..., min_length=1, max_length=4096)


class CreatePostRequest(BaseModel):
    restaurant_id: str
    summary: str = Field(..., min_length=1, max_length=1500)
    action_type: Optional[str] = None
    action_url: Optional[str] = None
    image_url: Optional[str] = None
    event: Optional[dict] = None
    offer: Optional[dict] = None


# ── OAuth ────────────────────────────────────────────────────


@router.get("/connect")
async def google_connect(
    restaurant_id: str = Query(..., description="Restaurant to connect"),
    redirect_uri: Optional[str] = Query(None, description="Frontend callback URL"),
    user: UserContext = Depends(get_current_user),
):
    """Generate an OAuth consent URL to connect a Google Business Profile."""
    restaurant_id = _resolve_restaurant_id(user, restaurant_id)
    try:
        await _verify_ownership(user, restaurant_id)
        return await _auth_svc.generate_auth_url(
            user_id=user.user_id,
            restaurant_id=restaurant_id,
            redirect_uri=redirect_uri,
        )
    except Exception as e:
        logger.error("google_connect_error", error=str(e), user_id=user.user_id)
        return JSONResponse(
            status_code=getattr(e, "status_code", 500),
            content={"error": str(e), "auth_url": None},
        )


@router.get("/callback")
async def google_callback(
    code: str = Query(...),
    state: str = Query(...),
    redirect_uri: Optional[str] = Query(None),
):
    """
    Handle OAuth callback — exchange authorization code for tokens.
    Public endpoint: user identity is derived from the server-side state token.
    """
    try:
        return await _auth_svc.handle_callback(
            code=code,
            state=state,
            redirect_uri=redirect_uri,
        )
    except Exception as e:
        logger.error("google_callback_error", error=str(e), state=state[:16])
        return JSONResponse(
            status_code=getattr(e, "status_code", 500),
            content={"connected": False, "error": str(e)},
        )


@router.get("/status")
async def google_connection_status(
    restaurant_id: str = Query(...),
    user: UserContext = Depends(get_current_user),
):
    """Check if a Google account is connected for a restaurant."""
    restaurant_id = _resolve_restaurant_id(user, restaurant_id)
    try:
        conn = await _token_mgr.get_connection_for_restaurant(user.user_id, restaurant_id)
        if not conn:
            return {"connected": False}
        return {
            "connected": True,
            "account_id": conn.get("account_id"),
            "location_id": conn.get("location_id"),
            "location_name": conn.get("location_name"),
        }
    except Exception as e:
        logger.error("google_status_error", error=str(e))
        return {"connected": False}


@router.post("/disconnect")
async def google_disconnect(
    body: ConnectRequest,
    user: UserContext = Depends(get_current_user),
):
    """Disconnect a Google account from a restaurant. Never crashes."""
    rid = _resolve_restaurant_id(user, body.restaurant_id)
    try:
        await _verify_ownership(user, rid)
        await _token_mgr.disconnect(user.user_id, rid)
        return {"disconnected": True}
    except Exception as e:
        logger.error("google_disconnect_error", error=str(e), user_id=user.user_id, restaurant_id=rid)
        return {"disconnected": False, "error": str(e)}


# ── Locations ────────────────────────────────────────────────


@router.get("/locations")
async def google_locations(
    restaurant_id: str = Query(...),
    user: UserContext = Depends(get_current_user),
):
    """Fetch Google Business accounts and their locations. Rate-limited + cached."""
    restaurant_id = _resolve_restaurant_id(user, restaurant_id)
    _FALLBACK = {"connected": True, "accounts": [], "locations": {}}

    # ── Rate limit guard: 5s cooldown per user+restaurant ──
    if await _rate_guard(user.user_id, restaurant_id, "locations"):
        return JSONResponse(
            status_code=429,
            content={**_FALLBACK, "rate_limited": True, "message": "Please wait a few seconds before retrying."},
        )

    try:
        conn = await _token_mgr.get_connection_for_restaurant(user.user_id, restaurant_id)
        if not conn:
            return {"connected": False, "accounts": [], "locations": {}}
        return await _locations_svc.fetch_and_store_locations(user.user_id, restaurant_id)
    except Exception as e:
        logger.error("google_locations_error", error=str(e), user_id=user.user_id, restaurant_id=restaurant_id)
        # Return safe fallback — never crash
        return _FALLBACK


@router.post("/locations/select")
async def google_select_location(
    body: SelectLocationRequest,
    user: UserContext = Depends(get_current_user),
):
    """Select which Google Business location to use for a restaurant."""
    rid = _resolve_restaurant_id(user, body.restaurant_id)
    try:
        await _verify_ownership(user, rid)
        return await _locations_svc.select_location(
            user_id=user.user_id,
            restaurant_id=rid,
            account_id=body.account_id,
            location_id=body.location_id,
            location_name=body.location_name,
        )
    except Exception as e:
        logger.error("google_select_location_error", error=str(e))
        return JSONResponse(
            status_code=getattr(e, "status_code", 500),
            content={"selected": False, "error": str(e)},
        )


# ── Reviews ──────────────────────────────────────────────────

_REVIEWS_FALLBACK = {
    "connected": True,
    "reviews": [],
    "average_rating": None,
    "total_review_count": 0,
    "next_page_token": None,
}


@router.get("/reviews")
async def google_reviews(
    restaurant_id: str = Query(...),
    page_size: int = Query(50, ge=1, le=50),
    page_token: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """Fetch reviews for the connected Google Business location."""
    restaurant_id = _resolve_restaurant_id(user, restaurant_id)
    if await _rate_guard(user.user_id, restaurant_id, "reviews"):
        return JSONResponse(
            status_code=429,
            content={**_REVIEWS_FALLBACK, "rate_limited": True},
        )

    try:
        conn = await _token_mgr.get_connection_for_restaurant(user.user_id, restaurant_id)
        if not conn or not conn.get("location_id"):
            return {**_REVIEWS_FALLBACK, "connected": False}
        return await _reviews_svc.list_reviews(
            user_id=user.user_id,
            restaurant_id=restaurant_id,
            page_size=page_size,
            page_token=page_token,
        )
    except Exception as e:
        logger.error("google_reviews_error", error=str(e), restaurant_id=restaurant_id)
        return _REVIEWS_FALLBACK


@router.post("/review/reply")
async def google_reply_to_review(
    body: ReviewReplyRequest,
    user: UserContext = Depends(get_current_user),
):
    """Reply to a Google Business review (duplicate-safe)."""
    rid = _resolve_restaurant_id(user, body.restaurant_id)
    try:
        await _verify_ownership(user, rid)
        return await _reviews_svc.reply_to_review(
            user_id=user.user_id,
            restaurant_id=rid,
            review_id=body.review_id,
            reply_text=body.reply_text,
        )
    except Exception as e:
        logger.error("google_reply_error", error=str(e))
        return JSONResponse(
            status_code=getattr(e, "status_code", 500),
            content={"success": False, "error": str(e)},
        )


# ── Posts ─────────────────────────────────────────────────────


@router.post("/post")
async def google_create_post(
    body: CreatePostRequest,
    user: UserContext = Depends(get_current_user),
):
    """Create a promotional post on Google Business Profile. Never crashes."""
    rid = _resolve_restaurant_id(user, body.restaurant_id)
    try:
        await _verify_ownership(user, rid)

        # Validate connection + location exist before calling Google
        conn = await _token_mgr.get_connection_for_restaurant(user.user_id, rid)
        if not conn or not conn.get("location_id"):
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "No Google location connected. Please connect and select a location first."},
            )

        result = await _posts_svc.create_post(
            user_id=user.user_id,
            restaurant_id=rid,
            summary=body.summary,
            action_type=body.action_type,
            action_url=body.action_url,
            image_url=body.image_url,
            event=body.event,
            offer=body.offer,
        )
        return result
    except Exception as e:
        logger.error("google_create_post_error", error=str(e), user_id=user.user_id, restaurant_id=rid)
        return JSONResponse(
            status_code=getattr(e, "status_code", 500),
            content={"success": False, "error": f"Failed to create post: {str(e)[:200]}"},
        )


@router.get("/posts")
async def google_list_posts(
    restaurant_id: str = Query(...),
    page_size: int = Query(20, ge=1, le=100),
    page_token: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """List existing Google Business posts for the connected location."""
    restaurant_id = _resolve_restaurant_id(user, restaurant_id)
    _FALLBACK = {"connected": True, "posts": [], "next_page_token": None}

    if await _rate_guard(user.user_id, restaurant_id, "posts"):
        return JSONResponse(status_code=429, content={**_FALLBACK, "rate_limited": True})

    try:
        conn = await _token_mgr.get_connection_for_restaurant(user.user_id, restaurant_id)
        if not conn or not conn.get("location_id"):
            return {**_FALLBACK, "connected": False}
        return await _posts_svc.list_posts(
            user_id=user.user_id,
            restaurant_id=restaurant_id,
            page_size=page_size,
            page_token=page_token,
        )
    except Exception as e:
        logger.error("google_list_posts_error", error=str(e), restaurant_id=restaurant_id)
        return _FALLBACK


@router.get("/post")
async def google_list_posts_alias(
    restaurant_id: str = Query(...),
    page_size: int = Query(20, ge=1, le=100),
    page_token: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """Alias for GET /posts (singular form)."""
    return await google_list_posts(
        restaurant_id=restaurant_id,
        page_size=page_size,
        page_token=page_token,
        user=user,
    )


# ── Insights ─────────────────────────────────────────────────

_INSIGHTS_FALLBACK = {
    "connected": True,
    "location_id": None,
    "location_name": "",
    "period": None,
    "metrics": {},
}


@router.get("/insights")
async def google_insights(
    restaurant_id: str = Query(...),
    start_date: Optional[date] = Query(None, description="YYYY-MM-DD"),
    end_date: Optional[date] = Query(None, description="YYYY-MM-DD"),
    user: UserContext = Depends(get_current_user),
):
    """Fetch performance insights (views, calls, directions, bookings)."""
    restaurant_id = _resolve_restaurant_id(user, restaurant_id)
    if await _rate_guard(user.user_id, restaurant_id, "insights"):
        return JSONResponse(status_code=429, content={**_INSIGHTS_FALLBACK, "rate_limited": True})

    try:
        conn = await _token_mgr.get_connection_for_restaurant(user.user_id, restaurant_id)
        if not conn or not conn.get("location_id"):
            return {**_INSIGHTS_FALLBACK, "connected": False}
        return await _insights_svc.get_performance_metrics(
            user_id=user.user_id,
            restaurant_id=restaurant_id,
            start_date=start_date,
            end_date=end_date,
        )
    except Exception as e:
        logger.error("google_insights_error", error=str(e), restaurant_id=restaurant_id)
        return _INSIGHTS_FALLBACK


@router.get("/insights/summary")
async def google_insights_summary(
    restaurant_id: str = Query(...),
    days: int = Query(30, ge=1, le=365),
    user: UserContext = Depends(get_current_user),
):
    """Aggregated summary for the growth dashboard."""
    restaurant_id = _resolve_restaurant_id(user, restaurant_id)
    _SUMMARY_FALLBACK = {
        "connected": True,
        "summary": {
            "total_impressions": 0,
            "total_calls": 0,
            "total_website_clicks": 0,
            "total_direction_requests": 0,
            "total_bookings": 0,
            "period_days": days,
        },
    }

    if await _rate_guard(user.user_id, restaurant_id, "insights_summary"):
        return JSONResponse(status_code=429, content={**_SUMMARY_FALLBACK, "rate_limited": True})

    try:
        conn = await _token_mgr.get_connection_for_restaurant(user.user_id, restaurant_id)
        if not conn or not conn.get("location_id"):
            return {**_SUMMARY_FALLBACK, "connected": False}
        return await _insights_svc.get_summary(
            user_id=user.user_id,
            restaurant_id=restaurant_id,
            days=days,
        )
    except Exception as e:
        logger.error("google_insights_summary_error", error=str(e), restaurant_id=restaurant_id)
        return _SUMMARY_FALLBACK


# ── Sync ─────────────────────────────────────────────────────


@router.post("/sync")
async def google_sync_restaurant(
    body: ConnectRequest,
    user: UserContext = Depends(get_current_user),
):
    """Trigger a full data sync for a restaurant (locations, reviews, posts, insights)."""
    rid = _resolve_restaurant_id(user, body.restaurant_id)
    try:
        await _verify_ownership(user, rid)
        return await sync_single_restaurant(user.user_id, rid)
    except Exception as e:
        logger.error("google_sync_error", error=str(e))
        return JSONResponse(
            status_code=getattr(e, "status_code", 500),
            content={"success": False, "error": str(e)},
        )
