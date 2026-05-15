"""
services.razorpay
=================

Modular Razorpay deep-integration package.

Phase 1 lays down the foundation: a single shared async HTTP client
(`RazorpayClient`) with retries, idempotency, structured logging and forensic
auditing into `rzp_api_calls` / `rzp_api_idempotency`, plus per-domain
sub-modules that subsequent phases fill in.

All sub-modules MUST go through `RazorpayClient` for outbound HTTPS — never
build their own `httpx.AsyncClient` — so we keep one chokepoint for retry,
audit, idempotency and rate-limit policy.
"""

from app.services.razorpay.client import (
    RazorpayClient,
    RazorpayError,
    RazorpayAPIError,
    RazorpayRateLimitedError,
    RazorpayServerError,
    RazorpayBadRequestError,
    get_razorpay_client,
)

__all__ = [
    "RazorpayClient",
    "RazorpayError",
    "RazorpayAPIError",
    "RazorpayRateLimitedError",
    "RazorpayServerError",
    "RazorpayBadRequestError",
    "get_razorpay_client",
]
