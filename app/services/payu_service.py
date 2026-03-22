"""
PayU Hosted Checkout — Payment Service.

Flow:
  1. Server generates HMAC-SHA512 hash of payment params
  2. Client POSTs form to PayU payment URL with hash
  3. PayU redirects back to surl/furl
  4. Server verifies reverse hash on callback

Hash formula:
  sha512(key|txnid|amount|productinfo|firstname|email|udf1|udf2|udf3|udf4|udf5||||||SALT)
"""
import hashlib

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _cfg():
    return get_settings()


def _payment_url() -> str:
    return "https://secure.payu.in/_payment"


class PayUService:

    def generate_hash(
        self,
        txnid: str,
        amount: str,
        productinfo: str,
        firstname: str,
        email: str,
        udf1: str = "",
        udf2: str = "",
        udf3: str = "",
        udf4: str = "",
        udf5: str = "",
    ) -> dict:
        """Generate PayU payment hash and return form data for redirect."""
        s = _cfg()
        hash_string = (
            f"{s.PAYU_MERCHANT_KEY}|{txnid}|{amount}|{productinfo}|"
            f"{firstname}|{email}|{udf1}|{udf2}|{udf3}|{udf4}|{udf5}||||||{s.PAYU_MERCHANT_SALT}"
        )
        payment_hash = hashlib.sha512(hash_string.encode()).hexdigest()

        logger.info("payu_hash_generated", txnid=txnid)
        return {
            "payment_url": _payment_url(),
            "key": s.PAYU_MERCHANT_KEY,
            "txnid": txnid,
            "amount": amount,
            "productinfo": productinfo,
            "firstname": firstname,
            "email": email,
            "hash": payment_hash,
            "udf1": udf1,
            "udf2": udf2,
            "udf3": udf3,
            "udf4": udf4,
            "udf5": udf5,
        }

    def verify_response_hash(
        self,
        status: str,
        email: str,
        firstname: str,
        productinfo: str,
        amount: str,
        txnid: str,
        response_hash: str,
        udf1: str = "",
        udf2: str = "",
        udf3: str = "",
        udf4: str = "",
        udf5: str = "",
    ) -> bool:
        """Verify PayU response hash (reverse hash)."""
        s = _cfg()
        # Reverse hash: SALT|status||||||udf5|udf4|udf3|udf2|udf1|email|firstname|productinfo|amount|txnid|key
        hash_string = (
            f"{s.PAYU_MERCHANT_SALT}|{status}||||||"
            f"{udf5}|{udf4}|{udf3}|{udf2}|{udf1}|{email}|{firstname}|"
            f"{productinfo}|{amount}|{txnid}|{s.PAYU_MERCHANT_KEY}"
        )
        expected = hashlib.sha512(hash_string.encode()).hexdigest()
        is_valid = expected == response_hash
        if not is_valid:
            logger.warning("payu_hash_mismatch", txnid=txnid)
        return is_valid
