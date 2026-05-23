"""
Retry utilities for external service calls (payments, APIs, webhooks)
and DB serialization-failure retries for SERIALIZABLE transactions.
Uses tenacity for configurable retry with exponential backoff.
"""
import httpx
import asyncpg
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    wait_random_exponential,
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


# ── DB serialization-failure retry ──────────────────────────────────────────
#
# PostgreSQL SERIALIZABLE transactions can fail with SQLSTATE 40001
# (`serialization_failure`) or 40P01 (`deadlock_detected`) when concurrent
# writers conflict. The contract is: client retries the transaction.
#
# Apply this decorator to async functions whose body is a single
# `async with get_serializable_transaction()` block. The decorator re-runs
# the whole function on serialization failure with jittered exponential
# backoff. Side effects after the transaction commit do NOT re-fire because
# the exception is raised from inside the transaction (rollback already
# happened). Side effects BEFORE the transaction (none in our code base)
# would re-fire, so place external mutations inside the txn or after it.
#
# Retries are observable via the `db_txn_serialization_retries_total`
# Prometheus counter (see `app/core/metrics.py`).

def _is_serialization_failure(exc: BaseException) -> bool:
    """True for Postgres SQLSTATE 40001 / 40P01 (asyncpg flavours)."""
    if isinstance(exc, (
        asyncpg.exceptions.SerializationError,
        asyncpg.exceptions.DeadlockDetectedError,
    )):
        return True
    # Some asyncpg paths surface as PostgresError with sqlstate attached
    sqlstate = getattr(exc, "sqlstate", None)
    return sqlstate in ("40001", "40P01")


def retry_on_serialization_failure(
    max_attempts: int = 4,
    min_wait: float = 0.02,
    max_wait: float = 1.0,
):
    """
    Retry an async function on Postgres serialization / deadlock failures.

    Increments the ``db_txn_serialization_retries_total`` counter on each
    retry attempt so we can alert on conflict storms.
    """
    from app.core.metrics import DB_TXN_SERIALIZATION_RETRIES  # local import: avoid cycle

    def _on_retry(retry_state):
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        sqlstate = getattr(exc, "sqlstate", "unknown")
        fn_name = retry_state.fn.__name__ if retry_state.fn else "anonymous"
        try:
            DB_TXN_SERIALIZATION_RETRIES.labels(sqlstate=sqlstate, function=fn_name).inc()
        except Exception:  # noqa: BLE001 — metrics failure must not break retry
            pass
        logger.warning(
            "db_serialization_retry fn=%s attempt=%d sqlstate=%s",
            fn_name, retry_state.attempt_number, sqlstate,
        )

    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_random_exponential(multiplier=min_wait, max=max_wait),
        retry=retry_if_exception_type((
            asyncpg.exceptions.SerializationError,
            asyncpg.exceptions.DeadlockDetectedError,
        )),
        before_sleep=_on_retry,
        reraise=True,
    )
