"""
PhonePe Standard Checkout — Payment Service.

Flow:
  1. Fetch OAuth2 access token (client_credentials grant)
  2. Create payment URL via /checkout/v2/pay
  3. Client redirects to PhonePe checkout
  4. Check order status via /checkout/v2/order/{id}/status
"""
import httpx
from datetime import datetime, timezone, timedelta

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_token_cache: dict = {"access_token": None, "expires_at": None}


def _cfg():
    return get_settings()


def _base_urls() -> tuple[str, str]:
    """Return (api_base, token_url)."""
    return (
        "https://api.phonepe.com/apis/pg",
        "https://api.phonepe.com/apis/identity-manager/v1/oauth/token",
    )


async def _get_access_token() -> str:
    """Fetch or return cached PhonePe OAuth2 token."""
    now = datetime.now(timezone.utc)
    if _token_cache["access_token"] and _token_cache["expires_at"] and _token_cache["expires_at"] > now:
        return _token_cache["access_token"]

    s = _cfg()
    _, token_url = _base_urls()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            token_url,
            data={
                "client_id": s.PHONEPE_CLIENT_ID,
                "client_secret": s.PHONEPE_CLIENT_SECRET,
                "client_version": s.PHONEPE_CLIENT_VERSION,
                "grant_type": "client_credentials",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + timedelta(seconds=data.get("expires_in", 1200) - 60)
    return data["access_token"]


class PhonePeService:

    async def create_order(
        self,
        merchant_order_id: str,
        amount_paise: int,
        redirect_url: str,
        message: str = "Payment for order",
        udf1: str = "",
    ) -> dict:
        """Create a PhonePe Standard Checkout payment URL."""
        token = await _get_access_token()
        api_base, _ = _base_urls()

        payload = {
            "merchantOrderId": merchant_order_id,
            "amount": amount_paise,
            "expireAfter": 1200,
            "metaInfo": {"udf1": udf1},
            "paymentFlow": {
                "type": "PG_CHECKOUT",
                "message": message,
                "merchantUrls": {"redirectUrl": redirect_url},
            },
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{api_base}/checkout/v2/pay",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"O-Bearer {token}",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        logger.info("phonepe_order_created", order_id=merchant_order_id)
        return data

    async def check_status(self, merchant_order_id: str) -> dict:
        """Check PhonePe order status."""
        token = await _get_access_token()
        api_base, _ = _base_urls()

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{api_base}/checkout/v2/order/{merchant_order_id}/status",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"O-Bearer {token}",
                },
            )
            resp.raise_for_status()
            return resp.json()
