"""
Auth router — Google OAuth via Supabase, token refresh, user profile, logout.

All endpoints are PUBLIC (no Bearer token required) except /me, /logout.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional

from app.core.auth import UserContext, get_current_user
from app.core.database import get_connection
from app.core.redis import cache_delete
from app.services.auth_service import (
    get_google_oauth_url,
    exchange_google_code,
    refresh_token,
    get_user,
    logout,
    AuthError,
    _initialize_restaurant_and_branch,
)
from app.services.rbac_service import rbac_service
from app.services.session_key_service import (
    issue_session_key,
    revoke_session_key,
    rotate_session_key,
)
from app.services.refresh_token_service import (
    ReuseDetected,
    parse_expires_at,
    refresh_token_service,
)

from app.core.logging import get_logger
router = APIRouter(prefix="/auth", tags=["Auth"])
_bearer = HTTPBearer()

logger = get_logger(__name__)


# ── Request / Response schemas ──────────────────────────────

class RefreshTokenRequest(BaseModel):
    refresh_token: str
    device_id: Optional[str] = Field(default=None, max_length=128)


class GoogleCallbackRequest(BaseModel):
    code: str
    device_id: Optional[str] = Field(default=None, max_length=128)


class IssueSigningKeyRequest(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=128)


# ── helpers ─────────────────────────────────────────────────

def _client_ip(request: Request) -> Optional[str]:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip() or None
    return request.client.host if request.client else None


def _user_agent(request: Request) -> Optional[str]:
    return (request.headers.get("user-agent") or "")[:200] or None


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
async def google_callback(request: Request, code: Optional[str] = None, body: Optional[GoogleCallbackRequest] = None):
    """
    Exchange the authorization code from Supabase Google OAuth callback for a session.
    The frontend calls this after being redirected back from Google with the `code` param.
    Returns access_token + refresh_token + user + session_signing_key.

    Backwards-compatible: legacy clients may pass `?code=...` as a query param;
    new clients should POST `{code, device_id}` so we can bind the HMAC key.
    """
    # Resolve effective inputs from either query or body
    effective_code = (body.code if body else None) or code
    device_id = body.device_id if body else None
    if not effective_code:
        return JSONResponse(status_code=400, content={"error": "missing_code"})
    try:
        result = await exchange_google_code(effective_code)
    except AuthError as e:
        return _handle_auth_error(e)

    # Issue per-(user, device) HMAC signing key + record refresh-token shadow
    try:
        user_id = (result.get("user") or {}).get("id") or result.get("user_id")
        if user_id and device_id:
            signing_key = await issue_session_key(user_id, device_id)
            result["session_signing_key"] = signing_key
            result["device_id"] = device_id
            refresh = result.get("refresh_token")
            if refresh:
                await refresh_token_service.record_issuance(
                    user_id=user_id,
                    device_id=device_id,
                    token=refresh,
                    expires_at=parse_expires_at(result),
                    ip=_client_ip(request),
                    user_agent=_user_agent(request),
                )
    except Exception as exc:
        # Never fail the login on key-issuance hiccups; the client will retry
        # via POST /auth/session-key/issue.
        logger.warning("session_key_issuance_failed", error=str(exc))

    return result


@router.post("/token/refresh")
async def refresh_access_token(request: Request, body: RefreshTokenRequest):
    """Get a new access_token using a refresh_token.

    Implements Refresh Token Rotation (RTR) with reuse detection:
      * The incoming token is checked against our refresh_tokens shadow.
      * If it was already rotated (i.e. someone replayed an old token),
        the entire chain for that (user, device) is revoked and the
        client is forced to re-authenticate.
      * On success, a brand-new refresh token + rotated HMAC signing key
        are issued atomically.
    """
    # 1. reuse detection — fail closed.
    try:
        await refresh_token_service.check_for_reuse(token=body.refresh_token)
    except ReuseDetected as exc:
        logger.warning(
            "refresh_token_reuse_blocked",
            user_id=exc.user_id,
            device_id=exc.device_id,
        )
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "code": "REFRESH_TOKEN_REUSE",
                    "message": "Refresh token has already been rotated — please sign in again.",
                    "retryable": False,
                }
            },
        )
    except Exception as exc:  # DB hiccup — degrade gracefully
        logger.warning("refresh_reuse_check_failed", error=str(exc))

    # 2. delegate to GoTrue.
    try:
        result = await refresh_token(body.refresh_token)
    except AuthError as e:
        return _handle_auth_error(e)

    # 3. record rotation + rotate HMAC key.
    try:
        user_id = (result.get("user") or {}).get("id") or result.get("user_id")
        new_refresh = result.get("refresh_token")
        if user_id and body.device_id and new_refresh:
            await refresh_token_service.record_issuance(
                user_id=user_id,
                device_id=body.device_id,
                token=new_refresh,
                parent_token=body.refresh_token,
                expires_at=parse_expires_at(result),
                ip=_client_ip(request),
                user_agent=_user_agent(request),
            )
            signing_key = await rotate_session_key(user_id, body.device_id)
            result["session_signing_key"] = signing_key
            result["device_id"] = body.device_id
    except Exception as exc:
        logger.warning("refresh_post_rotation_failed", error=str(exc))

    return result


@router.post("/session-key/issue")
async def issue_signing_key(
    body: IssueSigningKeyRequest,
    user: UserContext = Depends(get_current_user),
):
    """Bootstrap / refresh the per-device HMAC signing key.

    Clients that authenticated before HMAC signing existed (or that have
    rotated installs) can call this once with a freshly-generated stable
    device_id to obtain a key. Subsequent calls rotate the key.
    """
    key = await issue_session_key(user.user_id, body.device_id)
    return {"session_signing_key": key, "device_id": body.device_id}


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
                # Bust the 5-minute UserContext cache that get_current_user wrote
                # with restaurant_id=None on the very first authenticated request.
                # Without this, every subsequent endpoint (merchant-wallet,
                # merchant-ledger, etc.) keeps seeing the stale `None` until the
                # cache TTL expires and returns 422 "No restaurant is bound".
                try:
                    await cache_delete(f"user_ctx:{user.user_id}")
                except Exception:
                    pass

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


@router.get("/permissions/me")
async def get_my_permissions(
    user: UserContext = Depends(get_current_user),
):
    """Return the authenticated user's resolved permissions and meta constraints.

    Frontend uses this to adapt UI (hide buttons, cap discount sliders, etc.).
    """
    return await rbac_service.get_user_permissions(user)


@router.post("/initialize-restaurant")
async def initialize_restaurant(
    user: UserContext = Depends(get_current_user)
):
    """Initialize restaurant and primary branch for the current user (idempotent)."""
    try:
        result = await _initialize_restaurant_and_branch(user.user_id, email=user.email)
        # Same cache-bust as /me: the cached UserContext was written before the
        # restaurant existed, so it still says restaurant_id=None.
        try:
            await cache_delete(f"user_ctx:{user.user_id}")
        except Exception:
            pass
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
async def logout_endpoint(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    user: UserContext = Depends(get_current_user),
):
    """Invalidate current session, revoke HMAC key, mark refresh token logged-out."""
    # Best-effort device-id from header (set by Flutter on every request)
    device_id = request.headers.get("X-Device-Id")
    if device_id:
        try:
            await revoke_session_key(user.user_id, device_id)
        except Exception as exc:
            logger.warning("logout_key_revoke_failed", error=str(exc))
    # Optional refresh-token-hash revoke when client supplies it.
    rt = request.headers.get("X-Refresh-Token")
    if rt:
        try:
            await refresh_token_service.revoke_for_logout(token=rt)
        except Exception as exc:
            logger.warning("logout_refresh_revoke_failed", error=str(exc))
    try:
        result = await logout(credentials.credentials)
        return result
    except AuthError as e:
        return _handle_auth_error(e)
