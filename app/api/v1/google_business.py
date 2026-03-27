"""
Google Business Profile — API Routes.

Endpoints for OAuth, locations, reviews, posts, insights, and sync.
All mutating endpoints verify restaurant ownership before proceeding.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, get_current_user
from app.core.exceptions import NotFoundError
from app.services.google.auth import GoogleAuthService
from app.services.google.locations import GoogleLocationsService
from app.services.google.reviews import GoogleReviewsService
from app.services.google.posts import GooglePostsService
from app.services.google.insights import GoogleInsightsService
from app.services.google.token_manager import GoogleTokenManager
from app.services.google.sync import sync_single_restaurant

router = APIRouter(prefix="/google", tags=["Google Business Profile"])

_auth_svc = GoogleAuthService()
_locations_svc = GoogleLocationsService()
_reviews_svc = GoogleReviewsService()
_posts_svc = GooglePostsService()
_insights_svc = GoogleInsightsService()
_token_mgr = GoogleTokenManager()


# ── Helpers ──────────────────────────────────────────────────


async def _verify_ownership(user: UserContext, restaurant_id: str) -> None:
    """Ensure the authenticated user owns (or has access to) the restaurant."""
    await _token_mgr.verify_restaurant_ownership(user.user_id, restaurant_id)


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
    await _verify_ownership(user, restaurant_id)
    return await _auth_svc.generate_auth_url(
        restaurant_id,
        user_id=user.user_id,
        redirect_uri=redirect_uri,
    )


@router.get("/callback")
async def google_callback(
    code: str = Query(...),
    state: str = Query(...),
    redirect_uri: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """Handle OAuth callback — exchange authorization code for tokens."""
    return await _auth_svc.handle_callback(
        code=code,
        state=state,
        user_id=user.user_id,
        redirect_uri=redirect_uri,
    )


@router.get("/status")
async def google_connection_status(
    restaurant_id: str = Query(...),
    user: UserContext = Depends(get_current_user),
):
    """Check if a Google account is connected for a restaurant."""
    conn = await _token_mgr.get_connection_for_restaurant(user.user_id, restaurant_id)
    if not conn:
        return {"connected": False}
    return {
        "connected": True,
        "account_id": conn.get("account_id"),
        "location_id": conn.get("location_id"),
        "location_name": conn.get("location_name"),
    }


@router.post("/disconnect")
async def google_disconnect(
    body: ConnectRequest,
    user: UserContext = Depends(get_current_user),
):
    """Disconnect a Google account from a restaurant."""
    await _verify_ownership(user, body.restaurant_id)
    await _token_mgr.disconnect(user.user_id, body.restaurant_id)
    return {"disconnected": True}


# ── Locations ────────────────────────────────────────────────


@router.get("/locations")
async def google_locations(
    restaurant_id: str = Query(...),
    user: UserContext = Depends(get_current_user),
):
    """Fetch Google Business accounts and their locations."""
    conn = await _token_mgr.get_connection_for_restaurant(user.user_id, restaurant_id)
    if not conn:
        return {"connected": False, "accounts": [], "locations": {}}
    return await _locations_svc.fetch_and_store_locations(user.user_id, restaurant_id)


@router.post("/locations/select")
async def google_select_location(
    body: SelectLocationRequest,
    user: UserContext = Depends(get_current_user),
):
    """Select which Google Business location to use for a restaurant."""
    await _verify_ownership(user, body.restaurant_id)
    return await _locations_svc.select_location(
        user_id=user.user_id,
        restaurant_id=body.restaurant_id,
        account_id=body.account_id,
        location_id=body.location_id,
        location_name=body.location_name,
    )


# ── Reviews ──────────────────────────────────────────────────


@router.get("/reviews")
async def google_reviews(
    restaurant_id: str = Query(...),
    page_size: int = Query(50, ge=1, le=50),
    page_token: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """Fetch reviews for the connected Google Business location."""
    conn = await _token_mgr.get_connection_for_restaurant(user.user_id, restaurant_id)
    if not conn or not conn.get("location_id"):
        return {
            "connected": False,
            "reviews": [],
            "average_rating": None,
            "total_review_count": 0,
            "next_page_token": None,
        }
    return await _reviews_svc.list_reviews(
        user_id=user.user_id,
        restaurant_id=restaurant_id,
        page_size=page_size,
        page_token=page_token,
    )


@router.post("/review/reply")
async def google_reply_to_review(
    body: ReviewReplyRequest,
    user: UserContext = Depends(get_current_user),
):
    """Reply to a Google Business review (duplicate-safe)."""
    await _verify_ownership(user, body.restaurant_id)
    return await _reviews_svc.reply_to_review(
        user_id=user.user_id,
        restaurant_id=body.restaurant_id,
        review_id=body.review_id,
        reply_text=body.reply_text,
    )


# ── Posts ─────────────────────────────────────────────────────


@router.post("/post")
async def google_create_post(
    body: CreatePostRequest,
    user: UserContext = Depends(get_current_user),
):
    """Create a promotional post on Google Business Profile."""
    await _verify_ownership(user, body.restaurant_id)
    return await _posts_svc.create_post(
        user_id=user.user_id,
        restaurant_id=body.restaurant_id,
        summary=body.summary,
        action_type=body.action_type,
        action_url=body.action_url,
        image_url=body.image_url,
        event=body.event,
        offer=body.offer,
    )


@router.get("/posts")
async def google_list_posts(
    restaurant_id: str = Query(...),
    page_size: int = Query(20, ge=1, le=100),
    page_token: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """List existing Google Business posts for the connected location."""
    conn = await _token_mgr.get_connection_for_restaurant(user.user_id, restaurant_id)
    if not conn or not conn.get("location_id"):
        return {"connected": False, "posts": [], "next_page_token": None}
    return await _posts_svc.list_posts(
        user_id=user.user_id,
        restaurant_id=restaurant_id,
        page_size=page_size,
        page_token=page_token,
    )


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


@router.get("/insights")
async def google_insights(
    restaurant_id: str = Query(...),
    start_date: Optional[date] = Query(None, description="YYYY-MM-DD"),
    end_date: Optional[date] = Query(None, description="YYYY-MM-DD"),
    user: UserContext = Depends(get_current_user),
):
    """Fetch performance insights (views, calls, directions, bookings)."""
    conn = await _token_mgr.get_connection_for_restaurant(user.user_id, restaurant_id)
    if not conn or not conn.get("location_id"):
        return {
            "connected": False,
            "location_id": None,
            "location_name": "",
            "period": None,
            "metrics": {},
        }
    return await _insights_svc.get_performance_metrics(
        user_id=user.user_id,
        restaurant_id=restaurant_id,
        start_date=start_date,
        end_date=end_date,
    )


@router.get("/insights/summary")
async def google_insights_summary(
    restaurant_id: str = Query(...),
    days: int = Query(30, ge=1, le=365),
    user: UserContext = Depends(get_current_user),
):
    """Aggregated summary for the growth dashboard."""
    conn = await _token_mgr.get_connection_for_restaurant(user.user_id, restaurant_id)
    if not conn or not conn.get("location_id"):
        return {
            "connected": False,
            "summary": {
                "total_impressions": 0,
                "total_calls": 0,
                "total_website_clicks": 0,
                "total_direction_requests": 0,
                "total_bookings": 0,
                "period_days": days,
            },
        }
    return await _insights_svc.get_summary(
        user_id=user.user_id,
        restaurant_id=restaurant_id,
        days=days,
    )


# ── Sync ─────────────────────────────────────────────────────


@router.post("/sync")
async def google_sync_restaurant(
    body: ConnectRequest,
    user: UserContext = Depends(get_current_user),
):
    """Trigger a full data sync for a restaurant (locations, reviews, posts, insights)."""
    await _verify_ownership(user, body.restaurant_id)
    return await sync_single_restaurant(user.user_id, body.restaurant_id)
