"""
Google Business Profile — Locations Service.

Fetches Google Business accounts and their locations.
Caches in DB + Redis for fast reads. Validates location selection.
"""
from app.core.database import get_connection
from app.core.logging import get_logger
from app.core.exceptions import ValidationError
from app.core.events import DomainEvent, emit_and_publish
from app.services.google.token_manager import GoogleTokenManager
from app.services.google.api_client import (
    google_api, ACCOUNT_MGMT_BASE, BUSINESS_INFO_BASE, _cache_key,
)

logger = get_logger(__name__)

token_mgr = GoogleTokenManager()

# Cache locations for 1 hour
LOCATIONS_CACHE_TTL = 3600


class GoogleLocationsService:
    """Fetch and manage Google Business locations with caching and validation."""

    async def list_accounts(self, user_id: str, restaurant_id: str) -> list[dict]:
        """List all Google Business accounts the user has access to."""
        data = await google_api.request(
            "GET",
            f"{ACCOUNT_MGMT_BASE}/accounts",
            user_id,
            restaurant_id,
            cache_key=_cache_key("accounts", restaurant_id),
            cache_ttl=LOCATIONS_CACHE_TTL,
        )
        return data.get("accounts", [])

    async def list_locations(
        self, user_id: str, restaurant_id: str, account_id: str
    ) -> list[dict]:
        """List locations for a specific Google Business account."""
        data = await google_api.request(
            "GET",
            f"{BUSINESS_INFO_BASE}/accounts/{account_id}/locations",
            user_id,
            restaurant_id,
            params={"readMask": "name,title,storefrontAddress,websiteUri,phoneNumbers"},
            cache_key=_cache_key("locations", restaurant_id, account_id),
            cache_ttl=LOCATIONS_CACHE_TTL,
        )
        return data.get("locations", [])

    async def fetch_and_store_locations(
        self, user_id: str, restaurant_id: str
    ) -> dict:
        """
        Full flow: list accounts → list locations → persist to DB.
        Returns accounts and locations for the frontend to choose from.
        """
        accounts = await self.list_accounts(user_id, restaurant_id)
        if not accounts:
            return {"accounts": [], "locations": {}}

        result = {"accounts": accounts, "locations": {}}
        for acct in accounts:
            acct_name = acct.get("name", "")
            acct_id = acct_name.split("/")[-1] if "/" in acct_name else acct_name
            try:
                locations = await self.list_locations(user_id, restaurant_id, acct_id)
                result["locations"][acct_id] = locations

                # ── Persist to google_locations table ──
                await self._upsert_locations_db(restaurant_id, acct_id, locations)
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

        await token_mgr.update_sync_timestamp(user_id, restaurant_id, "locations")
        return result

    async def select_location(
        self,
        user_id: str,
        restaurant_id: str,
        account_id: str,
        location_id: str,
        location_name: str = "",
    ) -> dict:
        """
        Persist user's chosen account+location.
        Validates that location_id actually belongs to the account.
        """
        # ── Validate location exists ──
        valid = await self._validate_location(user_id, restaurant_id, account_id, location_id)
        if not valid:
            raise ValidationError(
                f"Location '{location_id}' not found under account '{account_id}'. "
                "Refresh your locations and try again."
            )

        await token_mgr.update_account_location(
            user_id=user_id,
            restaurant_id=restaurant_id,
            account_id=account_id,
            location_id=location_id,
            location_name=location_name,
        )

        await emit_and_publish(DomainEvent(
            event_type="google.location_selected",
            payload={
                "account_id": account_id,
                "location_id": location_id,
                "location_name": location_name,
            },
            user_id=user_id,
            restaurant_id=restaurant_id,
        ))

        logger.info(
            "google_location_selected",
            user_id=user_id,
            restaurant_id=restaurant_id,
            account_id=account_id,
            location_id=location_id,
        )
        return {
            "selected": True,
            "account_id": account_id,
            "location_id": location_id,
        }

    async def get_cached_locations(self, restaurant_id: str) -> list[dict]:
        """Read locations from DB (for fast reads without Google API call)."""
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT account_id, location_id, location_name, address, phone, website_uri, synced_at
                FROM google_locations
                WHERE restaurant_id = $1
                ORDER BY location_name
                """,
                restaurant_id,
            )
        return [dict(r) for r in rows]

    # ── Private ──────────────────────────────────────────────

    async def _validate_location(
        self, user_id: str, restaurant_id: str, account_id: str, location_id: str
    ) -> bool:
        """Check that location_id belongs to account_id by fetching live data."""
        # First check DB cache
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1 FROM google_locations
                WHERE restaurant_id = $1 AND account_id = $2 AND location_id = $3
                """,
                restaurant_id,
                account_id,
                location_id,
            )
        if row:
            return True

        # Fallback to live API
        try:
            locations = await self.list_locations(user_id, restaurant_id, account_id)
            for loc in locations:
                loc_name = loc.get("name", "")
                lid = loc_name.split("/")[-1] if "/" in loc_name else loc_name
                if lid == location_id:
                    return True
        except Exception:
            logger.warning("google_location_validation_fallback_failed")

        return False

    async def _upsert_locations_db(
        self, restaurant_id: str, account_id: str, locations: list[dict]
    ) -> None:
        """Persist fetched locations to DB for offline reads."""
        if not locations:
            return
        async with get_connection() as conn:
            for loc in locations:
                loc_name_raw = loc.get("name", "")
                loc_id = loc_name_raw.split("/")[-1] if "/" in loc_name_raw else loc_name_raw
                address = loc.get("storefrontAddress")
                phones = loc.get("phoneNumbers", {})
                phone = phones.get("primaryPhone", "") if isinstance(phones, dict) else ""
                await conn.execute(
                    """
                    INSERT INTO google_locations
                        (restaurant_id, account_id, location_id, location_name, address, phone, website_uri, raw_data, synced_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
                    ON CONFLICT (restaurant_id, account_id, location_id) DO UPDATE SET
                        location_name = EXCLUDED.location_name,
                        address       = EXCLUDED.address,
                        phone         = EXCLUDED.phone,
                        website_uri   = EXCLUDED.website_uri,
                        raw_data      = EXCLUDED.raw_data,
                        synced_at     = now()
                    """,
                    restaurant_id,
                    account_id,
                    loc_id,
                    loc.get("title", ""),
                    address if address else None,
                    phone,
                    loc.get("websiteUri", ""),
                    loc,
                )
