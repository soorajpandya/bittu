"""
Authentication service — proxies Supabase GoTrue Auth REST API.

Google OAuth is the primary auth flow. The backend provides:
  - Google OAuth URL generation
  - Code exchange (PKCE) for session tokens
  - Token refresh, user profile, logout
  - Restaurant/branch initialization on first login
"""
import httpx
import uuid
from typing import Optional
from urllib.parse import quote

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.database import get_connection

logger = get_logger(__name__)

# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def _auth_url(path: str) -> str:
    """Build full Supabase GoTrue URL."""
    s = get_settings()
    return f"{s.SUPABASE_URL}/auth/v1/{path.lstrip('/')}"


def _headers(*, use_service_role: bool = False) -> dict:
    s = get_settings()
    api_key = s.SUPABASE_SERVICE_ROLE_KEY if use_service_role else s.SUPABASE_ANON_KEY
    return {
        "apikey": api_key,
        "Content-Type": "application/json",
    }


def _bearer_headers(access_token: str) -> dict:
    h = _headers()
    h["Authorization"] = f"Bearer {access_token}"
    return h


async def _post(path: str, body: dict, *, service_role: bool = False) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _auth_url(path),
            json=body,
            headers=_headers(use_service_role=service_role),
        )
    data = resp.json() if resp.status_code != 204 else {}
    if resp.status_code >= 400:
        msg = data.get("error_description") or data.get("msg") or data.get("message") or str(data)
        logger.warning("supabase_auth_error", path=path, status=resp.status_code, detail=msg)
        raise AuthError(resp.status_code, msg)
    return data


async def _put(path: str, body: dict, *, service_role: bool = False) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            _auth_url(path),
            json=body,
            headers=_headers(use_service_role=service_role),
        )
    data = resp.json() if resp.status_code != 204 else {}
    if resp.status_code >= 400:
        msg = data.get("error_description") or data.get("msg") or data.get("message") or str(data)
        logger.warning("supabase_auth_error", path=path, status=resp.status_code, detail=msg)
        raise AuthError(resp.status_code, msg)
    return data


async def _get(path: str, *, token: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            _auth_url(path),
            headers=_bearer_headers(token),
        )
    data = resp.json() if resp.status_code != 204 else {}
    if resp.status_code >= 400:
        msg = data.get("error_description") or data.get("msg") or data.get("message") or str(data)
        raise AuthError(resp.status_code, msg)
    return data


class AuthError(Exception):
    """Wraps Supabase GoTrue error with HTTP status."""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ────────────────────────────────────────────────────────────
# Google OAuth
# ────────────────────────────────────────────────────────────

def get_google_oauth_url(redirect_to: str) -> str:
    """
    Build the Supabase OAuth authorize URL for Google.
    The frontend should redirect the user's browser to this URL.
    After Google consent, Supabase will redirect to `redirect_to` with the code.
    """
    s = get_settings()
    base = f"{s.SUPABASE_URL}/auth/v1/authorize"
    return f"{base}?provider=google&redirect_to={quote(redirect_to, safe='')}"


async def exchange_google_code(code: str) -> dict:
    """
    Exchange the authorization code from Supabase Google OAuth callback
    for a session (access_token + refresh_token).
    Automatically initializes restaurant and primary branch for the user.
    """
    s = get_settings()
    url = f"{s.SUPABASE_URL}/auth/v1/token?grant_type=pkce"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            url,
            json={"auth_code": code},
            headers=_headers(),
        )
    data = resp.json() if resp.status_code != 204 else {}
    if resp.status_code >= 400:
        msg = data.get("error_description") or data.get("msg") or data.get("message") or str(data)
        logger.warning("google_code_exchange_failed", status=resp.status_code, detail=msg)
        raise AuthError(resp.status_code, msg)

    user = data.get("user", {})
    user_id = user.get("id")
    email = user.get("email")

    logger.info("google_login", email=email, user_id=user_id)

    if user_id:
        # Auto-link: accept any pending staff invites for this email
        accepted = []
        try:
            from app.services.invite_service import invite_service
            accepted = await invite_service.accept_pending_invites(user_id, email)
            if accepted:
                logger.info("staff_invites_auto_accepted", user_id=user_id, count=len(accepted))
        except Exception as exc:
            logger.warning("invite_auto_link_failed", user_id=user_id, error=str(exc))

        # Initialize restaurant and primary branch for new/returning owner
        # (skip if user was just linked as staff — they're not an owner)
        if not accepted:
            await _initialize_restaurant_and_branch(user_id, email=email)

    return data


# ────────────────────────────────────────────────────────────
# Session management
# ────────────────────────────────────────────────────────────

async def refresh_token(refresh_token_str: str) -> dict:
    """
    Get a new access_token using a refresh_token.
    Returns fresh session.
    """
    body = {"refresh_token": refresh_token_str}
    result = await _post("token?grant_type=refresh_token", body)
    return result


async def get_user(access_token: str) -> dict:
    """Get currently authenticated user profile."""
    return await _get("user", token=access_token)


async def logout(access_token: str) -> dict:
    """Invalidate the user's session on Supabase."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _auth_url("logout"),
            headers=_bearer_headers(access_token),
        )
    if resp.status_code >= 400:
        data = resp.json() if resp.content else {}
        msg = data.get("error_description") or data.get("msg") or "Logout failed"
        raise AuthError(resp.status_code, msg)
    logger.info("user_logout")
    return {"message": "Logged out"}


async def update_user_metadata(user_id: str, metadata: dict) -> dict:
    """
    Update user app_metadata using Supabase admin API.
    Requires service role key.
    """
    return await _put(f"admin/users/{user_id}", metadata, service_role=True)


async def _initialize_restaurant_and_branch(user_id: str, email: Optional[str] = None) -> dict:
    """
    Initialize default restaurant and primary branch for a new user on first signup.
    
    Creates:
    - A restaurant owned by the user
    - A primary branch for that restaurant (is_main_branch=true)
    
    Returns dict with restaurant_id and branch_id.
    Idempotent: if called multiple times, returns existing IDs instead of creating duplicates.
    """
    try:
        async with get_connection() as conn:
            # Check if user already has a restaurant
            existing = await conn.fetchrow(
                """
                SELECT r.id as restaurant_id, sb.id as branch_id
                FROM restaurants r
                LEFT JOIN sub_branches sb ON sb.restaurant_id = r.id AND sb.is_main_branch = true
                WHERE r.owner_id = $1
                LIMIT 1
                """,
                user_id,
            )
            
            if existing:
                restaurant_id = str(existing["restaurant_id"])
                branch_id = str(existing["branch_id"]) if existing["branch_id"] else None
                logger.info("restaurant_already_exists", user_id=user_id, restaurant_id=restaurant_id, branch_id=branch_id)

                # If restaurant exists but branch is missing, create main branch now.
                if not branch_id:
                    branch_id = str(uuid.uuid4())
                    branch_name = "Main"
                    await conn.execute(
                        """
                        INSERT INTO sub_branches (id, restaurant_id, owner_id, name, is_main_branch, is_active, created_at)
                        VALUES ($1, $2, $3, $4, true, true, NOW())
                        """,
                        branch_id, restaurant_id, user_id, branch_name,
                    )
                    logger.info("branch_created_for_existing_restaurant", user_id=user_id, restaurant_id=restaurant_id, branch_id=branch_id)

                return {
                    "restaurant_id": restaurant_id,
                    "branch_id": branch_id,
                }
            
            # Create restaurant
            restaurant_id = str(uuid.uuid4())
            restaurant_name = email or f"Restaurant_{user_id[:8]}"  # Use email prefix or user ID
            
            await conn.execute(
                """
                INSERT INTO restaurants (id, owner_id, name, is_active, created_at)
                VALUES ($1, $2, $3, true, NOW())
                """,
                restaurant_id, user_id, restaurant_name,
            )
            
            # Create primary branch
            branch_id = str(uuid.uuid4())
            branch_name = "Main"  # Default primary branch name
            
            await conn.execute(
                """
                INSERT INTO sub_branches (id, restaurant_id, owner_id, name, is_main_branch, is_active, created_at)
                VALUES ($1, $2, $3, $4, true, true, NOW())
                """,
                branch_id, restaurant_id, user_id, branch_name,
            )
            
            logger.info(
                "restaurant_initialized",
                user_id=user_id,
                restaurant_id=restaurant_id,
                branch_id=branch_id,
                email=email,
            )
            
            # Update user's user_metadata with restaurant and branch IDs
            try:
                await update_user_metadata(user_id, {
                    "user_metadata": {
                        "restaurant_id": restaurant_id,
                        "branch_id": branch_id,
                        "owner_id": user_id,
                        "is_branch_user": False,
                    }
                })
                logger.info("user_metadata_updated", user_id=user_id)
            except Exception as e:
                logger.warning("failed_to_update_user_metadata", user_id=user_id, error=str(e))
            
            return {
                "restaurant_id": restaurant_id,
                "branch_id": branch_id,
            }
            
    except Exception as e:
        logger.warning(
            "failed_to_initialize_restaurant",
            user_id=user_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        # Don't raise — let signup complete even if restaurant init fails
        # User can set up restaurant manually later
        return {"restaurant_id": None, "branch_id": None}
