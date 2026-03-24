"""
Auth router — Google OAuth via Supabase, token refresh, user profile, logout.

All endpoints are PUBLIC (no Bearer token required) except /me, /logout.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from app.core.auth import UserContext, get_current_user
from app.core.database import get_connection
from app.services.auth_service import (
    get_google_oauth_url,
    exchange_google_code,
    refresh_token,
    get_user,
    logout,
    AuthError,
    _initialize_restaurant_and_branch,
)

from app.core.logging import get_logger
router = APIRouter(prefix="/auth", tags=["Auth"])
_bearer = HTTPBearer()

logger = get_logger(__name__)


# ── Request / Response schemas ──────────────────────────────

class RefreshTokenRequest(BaseModel):
    refresh_token: str


# ── Error handler ───────────────────────────────────────────

def _handle_auth_error(e: AuthError):
    return JSONResponse(status_code=e.status_code, content={"error": e.detail})


# ── PUBLIC endpoints (no token required) ────────────────────

@router.get("/google")
async def google_redirect(redirect_to: str):
    """
    Returns the Supabase Google OAuth URL.
    Frontend should redirect the user's browser to the returned URL.
    `redirect_to` is where Supabase sends the user after Google consent (your frontend callback page).
    """
    url = get_google_oauth_url(redirect_to)
    return {"url": url}


@router.post("/google/callback")
async def google_callback(code: str):
    """
    Exchange the authorization code from Supabase Google OAuth callback for a session.
    The frontend calls this after being redirected back from Google with the `code` param.
    Returns access_token + refresh_token + user.
    """
    try:
        result = await exchange_google_code(code)
        return result
    except AuthError as e:
        return _handle_auth_error(e)


@router.post("/token/refresh")
async def refresh_access_token(body: RefreshTokenRequest):
    """Get a new access_token using a refresh_token."""
    try:
        result = await refresh_token(body.refresh_token)
        return result
    except AuthError as e:
        return _handle_auth_error(e)


# ── PROTECTED endpoints (Bearer token required) ────────────

@router.get("/me")
async def get_me(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    user: UserContext = Depends(get_current_user)
):
    """Get current user profile with branch_id. Requires access_token."""
    try:
        # Ensure owner has a restaurant and primary branch initialized.
        if not user.branch_id:
            init = await _initialize_restaurant_and_branch(user.user_id, email=user.email)
            if init.get("branch_id"):
                user.branch_id = init["branch_id"]
                user.restaurant_id = init.get("restaurant_id")
                user.owner_id = user.user_id
                user.is_branch_user = False

        # Get Supabase user profile
        supabase_user = await get_user(credentials.credentials)

        # Merge with internal user context
        result = {
            **supabase_user,
            "branch_id": user.branch_id,
            "restaurant_id": user.restaurant_id,
            "role": user.role,
            "owner_id": user.owner_id,
            "is_branch_user": user.is_branch_user,
        }
        return result
    except AuthError as e:
        return _handle_auth_error(e)


@router.post("/initialize-restaurant")
async def initialize_restaurant(
    user: UserContext = Depends(get_current_user)
):
    """Initialize restaurant and primary branch for the current user (idempotent)."""
    try:
        result = await _initialize_restaurant_and_branch(user.user_id, email=user.email)
        return result
    except Exception as e:
        logger.error("initialize_restaurant_failed", user_id=user.user_id, error=str(e))
        return JSONResponse(
            status_code=500,
            content={"error": "Failed to initialize restaurant", "details": str(e)}
        )


@router.get("/debug-db")
async def debug_db():
    """Debug database connectivity."""
    try:
        async with get_connection() as conn:
            result = await conn.fetchval("SELECT 1")
            return {"db_connected": True, "test_query": result}
    except RuntimeError as e:
        if "Database pool not initialized" in str(e):
            return JSONResponse(
                status_code=503,
                content={
                    "db_connected": False,
                    "error": "Database pool not initialized during startup",
                    "error_type": "RuntimeError",
                    "suggestion": "Check startup logs for db_connect_failed. Fix DATABASE_URL or network connectivity."
                }
            )
        else:
            raise
    except Exception as e:
        logger.error("db_debug_failed", error=str(e), error_type=type(e).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "db_connected": False,
                "error": str(e),
                "error_type": type(e).__name__,
                "suggestion": "Check DATABASE_URL, network connectivity, and Supabase credentials"
            }
        )


@router.post("/logout")
async def logout_endpoint(credentials: HTTPAuthorizationCredentials = Depends(_bearer)):
    """Invalidate current session."""
    try:
        result = await logout(credentials.credentials)
        return result
    except AuthError as e:
        return _handle_auth_error(e)
