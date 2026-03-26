"""
Google Business Profile — OAuth Authentication Service.

Handles:
  - OAuth URL generation
  - Callback code exchange
"""
import secrets
import urllib.parse
import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.exceptions import ValidationError
from app.services.google.token_manager import GoogleTokenManager

logger = get_logger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/business.manage"

token_mgr = GoogleTokenManager()


class GoogleAuthService:
    """OAuth 2.0 flow for Google Business Profile."""

    def generate_auth_url(self, restaurant_id: str) -> dict:
        """
        Build the Google OAuth consent URL.
        The `state` param carries restaurant_id for post-callback routing.
        """
        settings = get_settings()
        state = f"{restaurant_id}:{secrets.token_urlsafe(16)}"
        params = {
            "client_id": settings.GOOGLE_BUSINESS_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_BUSINESS_REDIRECT_URI,
            "response_type": "code",
            "scope": GOOGLE_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
        return {"auth_url": url, "state": state}

    async def handle_callback(
        self, code: str, state: str, user_id: str
    ) -> dict:
        """
        Exchange authorization code for tokens and store them.
        Returns the stored connection info.
        """
        # Extract restaurant_id from state
        parts = state.split(":", 1)
        if not parts:
            raise ValidationError("Invalid OAuth state parameter")
        restaurant_id = parts[0]

        settings = get_settings()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_BUSINESS_CLIENT_ID,
                    "client_secret": settings.GOOGLE_BUSINESS_CLIENT_SECRET,
                    "redirect_uri": settings.GOOGLE_BUSINESS_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )

        if resp.status_code != 200:
            logger.error("google_oauth_exchange_failed", status=resp.status_code, body=resp.text)
            raise ValidationError(f"Google OAuth token exchange failed: {resp.text}")

        data = resp.json()
        access_token = data["access_token"]
        refresh_token = data.get("refresh_token", "")
        expires_in = data.get("expires_in", 3600)

        if not refresh_token:
            logger.warning(
                "google_no_refresh_token",
                user_id=user_id,
                hint="User may have already granted access. Revoke and retry.",
            )

        connection = await token_mgr.store_tokens(
            user_id=user_id,
            restaurant_id=restaurant_id,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
        )
        logger.info("google_oauth_complete", user_id=user_id, restaurant_id=restaurant_id)
        return {
            "connected": True,
            "restaurant_id": restaurant_id,
            "id": str(connection["id"]),
        }
