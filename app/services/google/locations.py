"""
Google Business Profile — Locations Service.

Fetches Google Business accounts and their locations.
"""
import httpx

from app.core.logging import get_logger
from app.core.exceptions import AppException
from app.services.google.token_manager import GoogleTokenManager

logger = get_logger(__name__)

BUSINESS_INFO_BASE = "https://mybusinessbusinessinformation.googleapis.com/v1"
ACCOUNT_MGMT_BASE = "https://mybusinessaccountmanagement.googleapis.com/v1"

token_mgr = GoogleTokenManager()


class GoogleLocationsService:
    """Fetch and manage Google Business locations."""

    async def list_accounts(self, user_id: str, restaurant_id: str) -> list[dict]:
        """List all Google Business accounts the user has access to."""
        access_token = await token_mgr.get_valid_token(user_id, restaurant_id)

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{ACCOUNT_MGMT_BASE}/accounts",
                headers={"Authorization": f"Bearer {access_token}"},
            )

        if resp.status_code != 200:
            logger.error("google_list_accounts_failed", status=resp.status_code, body=resp.text)
            raise AppException(
                status_code=resp.status_code,
                detail=f"Failed to list Google accounts: {resp.text}",
                error_code="GOOGLE_API_ERROR",
            )

        data = resp.json()
        return data.get("accounts", [])

    async def list_locations(
        self, user_id: str, restaurant_id: str, account_id: str
    ) -> list[dict]:
        """List locations for a specific Google Business account."""
        access_token = await token_mgr.get_valid_token(user_id, restaurant_id)

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{BUSINESS_INFO_BASE}/accounts/{account_id}/locations",
                params={"readMask": "name,title,storefrontAddress,websiteUri,phoneNumbers"},
                headers={"Authorization": f"Bearer {access_token}"},
            )

        if resp.status_code != 200:
            logger.error("google_list_locations_failed", status=resp.status_code, body=resp.text)
            raise AppException(
                status_code=resp.status_code,
                detail=f"Failed to list locations: {resp.text}",
                error_code="GOOGLE_API_ERROR",
            )

        data = resp.json()
        return data.get("locations", [])

    async def fetch_and_store_locations(
        self, user_id: str, restaurant_id: str
    ) -> dict:
        """
        Full flow: list accounts → list locations → store first account/location.
        Returns accounts and locations for the frontend to choose from.
        """
        accounts = await self.list_accounts(user_id, restaurant_id)
        if not accounts:
            return {"accounts": [], "locations": []}

        # For each account, fetch locations
        result = {"accounts": accounts, "locations": {}}
        for acct in accounts:
            acct_name = acct.get("name", "")  # e.g. "accounts/123"
            acct_id = acct_name.split("/")[-1] if "/" in acct_name else acct_name
            try:
                locations = await self.list_locations(user_id, restaurant_id, acct_id)
                result["locations"][acct_id] = locations
            except Exception as e:
                logger.warning("google_location_fetch_error", account_id=acct_id, error=str(e))
                result["locations"][acct_id] = []

        # Auto-select if only one account with one location
        all_locs = [
            (aid, loc)
            for aid, locs in result["locations"].items()
            for loc in locs
        ]
        if len(accounts) == 1 and len(all_locs) == 1:
            acct_id, loc = all_locs[0]
            loc_name = loc.get("name", "")
            loc_id = loc_name.split("/")[-1] if "/" in loc_name else loc_name
            await token_mgr.update_account_location(
                user_id=user_id,
                restaurant_id=restaurant_id,
                account_id=acct_id,
                location_id=loc_id,
                location_name=loc.get("title", ""),
            )

        return result

    async def select_location(
        self,
        user_id: str,
        restaurant_id: str,
        account_id: str,
        location_id: str,
        location_name: str = "",
    ) -> dict:
        """Persist user's chosen account+location."""
        await token_mgr.update_account_location(
            user_id=user_id,
            restaurant_id=restaurant_id,
            account_id=account_id,
            location_id=location_id,
            location_name=location_name,
        )
        return {
            "selected": True,
            "account_id": account_id,
            "location_id": location_id,
        }
