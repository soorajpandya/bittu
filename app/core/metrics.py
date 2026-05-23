"""
Prometheus metrics for application observability.
Exposes /metrics endpoint for Prometheus scraping.
"""
from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    generate_latest,
    CONTENT_TYPE_LATEST,
)
from starlette.requests import Request
from starlette.responses import Response

# ── Request metrics ──
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status_code"],
)

REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

# ── Business metrics ──
ORDERS_CREATED = Counter(
    "orders_created_total",
    "Total orders created",
    ["order_type"],
)

PAYMENTS_PROCESSED = Counter(
    "payments_processed_total",
    "Total payments processed",
    ["gateway", "status"],
)

ACTIVE_WEBSOCKETS = Gauge(
    "active_websocket_connections",
    "Current number of active WebSocket connections",
)

DB_POOL_SIZE = Gauge(
    "db_pool_size",
    "Current DB connection pool size",
)

EXTERNAL_API_CALLS = Counter(
    "external_api_calls_total",
    "Total external API calls",
    ["service", "status"],
)

EXTERNAL_API_DURATION = Histogram(
    "external_api_call_duration_seconds",
    "External API call duration",
    ["service"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

RATE_LIMIT_HITS = Counter(
    "rate_limit_hits_total",
    "Total rate limit rejections",
)

AUTH_FAILURES = Counter(
    "auth_failures_total",
    "Total authentication failures",
    ["reason"],
)

# ── DB pool / transaction observability ─────────────────────────────────────
# Histogram of seconds spent waiting for a pool slot (NOT query duration).
# Watch p95/p99 — anything > 0.05s sustained signals pool starvation; either
# bump DB_POOL_SIZE or scale out workers / introduce PgBouncer.
DB_POOL_ACQUIRE_WAIT_SECONDS = Histogram(
    "db_pool_acquire_wait_seconds",
    "Seconds spent waiting to acquire a connection from the asyncpg pool",
    ["txn_kind"],  # connection | transaction | serializable
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

# Counter of SERIALIZABLE-transaction retries due to SQLSTATE 40001/40P01.
# Alert when rate > N/min — indicates hot-row write contention.
DB_TXN_SERIALIZATION_RETRIES = Counter(
    "db_txn_serialization_retries_total",
    "Total SERIALIZABLE transaction retries (40001 / 40P01)",
    ["sqlstate", "function"],
)


async def metrics_endpoint(request: Request) -> Response:
    """Prometheus metrics endpoint."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
