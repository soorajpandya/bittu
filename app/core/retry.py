"""
Retry utilities for external service calls (payments, APIs, webhooks).
Uses tenacity for configurable retry with exponential backoff.
"""
import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)
import logging

logger = logging.getLogger(__name__)


def retry_external_call(
    max_attempts: int = 3,
    min_wait: float = 0.5,
    max_wait: float = 10.0,
):
    """
    Decorator for retrying external API calls with exponential backoff.
    Only retries on transient errors (timeouts, 5xx, connection errors).
    """
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        retry=retry_if_exception_type((
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
            ConnectionError,
            TimeoutError,
        )),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


def retry_payment_call(max_attempts: int = 3):
    """
    Stricter retry for payment operations.
    Only retries on network-level failures, NOT on 4xx responses.
    """
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        retry=retry_if_exception_type((
            httpx.TimeoutException,
            httpx.ConnectError,
            ConnectionError,
            TimeoutError,
        )),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
