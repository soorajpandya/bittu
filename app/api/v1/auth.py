"""
Auth router — public endpoints for signup, login, OTP, password reset, token refresh.

All endpoints are PUBLIC (no Bearer token required) except /me, /logout, /update-password.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field
from typing import Optional

from app.core.auth import UserContext, get_current_user
from app.core.database import get_connection
from app.services.auth_service import (
    signup_with_email,
    signup_with_phone,
    login_with_email,
    login_with_phone,
    send_otp,
    send_email_otp,
    verify_otp,
    verify_email_otp,
    refresh_token,
    forgot_password,
    update_password,
    get_user,
    logout,
    AuthError,
    _initialize_restaurant_and_branch,

    login_with_phone,
    send_otp,
    send_email_otp,
    verify_otp,
    verify_email_otp,
    refresh_token,
    forgot_password,
    update_password,
    get_user,
    logout,
    AuthError,
)

from app.core.logging import get_logger
router = APIRouter(prefix="/auth", tags=["Auth"])
_bearer = HTTPBearer()

logger = get_logger(__name__)


# ── Request / Response schemas ──────────────────────────────

class EmailSignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=6)
    full_name: Optional[str] = None
    phone: Optional[str] = None

class EmailLoginRequest(BaseModel):
    email: EmailStr
    password: str

class PhoneSignupRequest(BaseModel):
    phone: str = Field(..., pattern=r"^\+\d{10,15}$")
    password: str = Field(..., min_length=6)
    full_name: Optional[str] = None

class PhoneLoginRequest(BaseModel):
    phone: str = Field(..., pattern=r"^\+\d{10,15}$")
    password: str

class SendOtpRequest(BaseModel):
    phone: str = Field(..., pattern=r"^\+\d{10,15}$")

class SendEmailOtpRequest(BaseModel):
    email: EmailStr

class VerifyOtpRequest(BaseModel):
    phone: str = Field(..., pattern=r"^\+\d{10,15}$")
    token: str

class VerifyEmailOtpRequest(BaseModel):
    email: EmailStr
    token: str

class RefreshTokenRequest(BaseModel):
    refresh_token: str

class ForgotPasswordRequest(BaseModel):
    email: EmailStr
    redirect_to: Optional[str] = None

class UpdatePasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=6)


# ── Error handler ───────────────────────────────────────────

def _handle_auth_error(e: AuthError):
    return JSONResponse(status_code=e.status_code, content={"error": e.detail})


# ── PUBLIC endpoints (no token required) ────────────────────

@router.post("/signup/email")
async def signup_email(body: EmailSignupRequest):
    """Register with email + password. Returns session with access_token."""
    try:
        metadata = {}
        if body.full_name:
            metadata["full_name"] = body.full_name
        if body.phone:
            metadata["phone"] = body.phone
        result = await signup_with_email(body.email, body.password, metadata or None)
        return result
    except AuthError as e:
        return _handle_auth_error(e)


@router.post("/signup/phone")
async def signup_phone(body: PhoneSignupRequest):
    """Register with phone + password."""
    try:
        metadata = {}
        if body.full_name:
            metadata["full_name"] = body.full_name
        result = await signup_with_phone(body.phone, body.password, metadata or None)
        return result
    except AuthError as e:
        return _handle_auth_error(e)


@router.post("/login/email")
async def login_email(body: EmailLoginRequest):
    """Login with email + password. Returns access_token + refresh_token."""
    try:
        result = await login_with_email(body.email, body.password)
        return result
    except AuthError as e:
        return _handle_auth_error(e)


@router.post("/login/phone")
async def login_phone(body: PhoneLoginRequest):
    """Login with phone + password."""
    try:
        result = await login_with_phone(body.phone, body.password)
        return result
    except AuthError as e:
        return _handle_auth_error(e)


@router.post("/otp/send")
async def send_phone_otp(body: SendOtpRequest):
    """Send OTP to phone for passwordless login."""
    try:
        result = await send_otp(body.phone)
        return {"message": "OTP sent", **result}
    except AuthError as e:
        return _handle_auth_error(e)


@router.post("/otp/send-email")
async def send_email_magic_link(body: SendEmailOtpRequest):
    """Send OTP / magic link to email."""
    try:
        result = await send_email_otp(body.email)
        return {"message": "OTP sent to email", **result}
    except AuthError as e:
        return _handle_auth_error(e)


@router.post("/otp/verify")
async def verify_phone_otp(body: VerifyOtpRequest):
    """Verify phone OTP. Returns session with access_token."""
    try:
        result = await verify_otp(body.phone, body.token)
        return result
    except AuthError as e:
        return _handle_auth_error(e)


@router.post("/otp/verify-email")
async def verify_email_otp_endpoint(body: VerifyEmailOtpRequest):
    """Verify email OTP. Returns session with access_token."""
    try:
        result = await verify_email_otp(body.email, body.token)
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


@router.post("/forgot-password")
async def forgot_password_endpoint(body: ForgotPasswordRequest):
    """Send password-reset email."""
    try:
        result = await forgot_password(body.email, body.redirect_to)
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


@router.post("/update-password")
async def update_password_endpoint(
    body: UpdatePasswordRequest,
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
):
    """Update password (user must be logged in)."""
    try:
        result = await update_password(credentials.credentials, body.new_password)
        return result
    except AuthError as e:
        return _handle_auth_error(e)


@router.post("/logout")
async def logout_endpoint(credentials: HTTPAuthorizationCredentials = Depends(_bearer)):
    """Invalidate current session."""
    try:
        result = await logout(credentials.credentials)
        return result
    except AuthError as e:
        return _handle_auth_error(e)
