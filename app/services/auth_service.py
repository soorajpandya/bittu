"""
Authentication service — proxies Supabase GoTrue Auth REST API.

All auth flows (signup, login, OTP, password-reset, token refresh, logout)
go through the backend so the frontend never touches Supabase credentials
directly and the backend can add audit logging / rate-limiting.
"""
import httpx
import uuid
from typing import Optional

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
# Public API
# ────────────────────────────────────────────────────────────

async def signup_with_email(email: str, password: str, metadata: Optional[dict] = None) -> dict:
    """
    Register a new user with email + password.
    Returns session (access_token, refresh_token, user).
    Automatically initializes restaurant and primary branch.
    """
    body: dict = {"email": email, "password": password}
    if metadata:
        body["data"] = metadata
    result = await _post("signup", body)
    logger.info("user_signup", email=email)
    
    # Initialize restaurant and primary branch for new user
    if result.get("user", {}).get("id"):
        user_id = result["user"]["id"]
        await _initialize_restaurant_and_branch(user_id, email=email)
    
    return result


async def login_with_email(email: str, password: str) -> dict:
    """
    Login with email + password.
    Returns session (access_token, refresh_token, user).
    """
    body = {"email": email, "password": password}
    result = await _post("token?grant_type=password", body)
    logger.info("user_login", email=email)
    return result


async def login_with_phone(phone: str, password: str) -> dict:
    """Login with phone + password."""
    body = {"phone": phone, "password": password}
    result = await _post("token?grant_type=password", body)
    logger.info("user_login_phone", phone=phone)
    return result


async def send_otp(phone: str) -> dict:
    """
    Send OTP to a phone number for passwordless login.
    If user doesn't exist, Supabase creates them automatically.
    """
    body = {"phone": phone}
    result = await _post("otp", body)
    logger.info("otp_sent", phone=phone)
    return result


async def send_email_otp(email: str) -> dict:
    """Send magic-link / OTP to email for passwordless login."""
    body = {"email": email}
    result = await _post("otp", body)
    logger.info("email_otp_sent", email=email)
    return result


async def verify_otp(phone: str, otp_token: str) -> dict:
    """
    Verify phone OTP.
    Returns session (access_token, refresh_token, user).
    Automatically initializes restaurant and primary branch for new users.
    """
    body = {"phone": phone, "token": otp_token, "type": "sms"}
    result = await _post("verify", body)
    logger.info("otp_verified", phone=phone)
    
    # Initialize restaurant and primary branch for new user
    if result.get("user", {}).get("id"):
        user_id = result["user"]["id"]
        await _initialize_restaurant_and_branch(user_id, email=None)
    
    return result


async def verify_email_otp(email: str, otp_token: str) -> dict:
    """
    Verify email OTP / magic-link token.
    Automatically initializes restaurant and primary branch for new users.
    """
    body = {"email": email, "token": otp_token, "type": "email"}
    result = await _post("verify", body)
    logger.info("email_otp_verified", email=email)
    
    # Initialize restaurant and primary branch for new user
    if result.get("user", {}).get("id"):
        user_id = result["user"]["id"]
        await _initialize_restaurant_and_branch(user_id, email=email)
    
    return result


async def refresh_token(refresh_token_str: str) -> dict:
    """
    Get a new access_token using a refresh_token.
    Returns fresh session.
    """
    body = {"refresh_token": refresh_token_str}
    result = await _post("token?grant_type=refresh_token", body)
    return result


async def forgot_password(email: str, redirect_to: Optional[str] = None) -> dict:
    """Send password-reset email."""
    body: dict = {"email": email}
    if redirect_to:
        body["redirect_to"] = redirect_to
    # Uses service role to bypass rate limits on reset emails
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _auth_url("recover"),
            json=body,
            headers=_headers(use_service_role=True),
        )
    data = resp.json() if resp.status_code != 204 else {}
    if resp.status_code >= 400:
        msg = data.get("error_description") or data.get("msg") or str(data)
        raise AuthError(resp.status_code, msg)
    logger.info("password_reset_sent", email=email)
    return {"message": "Password reset email sent"}


async def update_password(access_token: str, new_password: str) -> dict:
    """Update password for currently logged-in user (requires valid access_token)."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            _auth_url("user"),
            json={"password": new_password},
            headers=_bearer_headers(access_token),
        )
    data = resp.json() if resp.status_code != 204 else {}
    if resp.status_code >= 400:
        msg = data.get("error_description") or data.get("msg") or str(data)
        raise AuthError(resp.status_code, msg)
    logger.info("password_updated")
    return data


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


async def signup_with_phone(phone: str, password: str, metadata: Optional[dict] = None) -> dict:
    """
    Register a new user with phone + password.
    Automatically initializes restaurant and primary branch.
    """
    body: dict = {"phone": phone, "password": password}
    if metadata:
        body["data"] = metadata
    result = await _post("signup", body)
    logger.info("user_signup_phone", phone=phone)
    
    # Initialize restaurant and primary branch for new user
    if result.get("user", {}).get("id"):
        user_id = result["user"]["id"]
        await _initialize_restaurant_and_branch(user_id, email=None)
    
    return result


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
