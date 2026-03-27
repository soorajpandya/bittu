"""
Middleware stack: rate limiting, request ID, error handling, audit logging, security headers, subscription check.
"""
import time
import uuid
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
import structlog

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.exceptions import AppException

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────
# Security Headers
# ──────────────────────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject production security headers into every response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        settings = get_settings()
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
            response.headers["Content-Security-Policy"] = "default-src 'self'"
        return response


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Inject a unique request ID into every request for tracing."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log every request with timing, user context, and trace info."""

    SENSITIVE_PATHS = {"/auth/login", "/auth/register", "/auth/refresh"}

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start_time = time.perf_counter()
        client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or (
            request.client.host if request.client else "unknown"
        )

        response = await call_next(request)

        duration_ms = (time.perf_counter() - start_time) * 1000
        request_id = getattr(request.state, "request_id", None)

        log_data = {
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round(duration_ms, 2),
            "client_ip": client_ip,
            "request_id": request_id,
            "user_agent": request.headers.get("user-agent", "")[:200],
        }

        if response.status_code >= 500:
            logger.error("http_request", **log_data)
        elif response.status_code >= 400:
            logger.warning("http_request", **log_data)
        else:
            logger.info("http_request", **log_data)

        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Redis-backed rate limiting per real client IP.
    Skips health checks and WebSocket upgrades.
    Uses X-Forwarded-For when behind a trusted proxy.
    """

    SKIP_PATHS = {"/health", "/metrics", "/docs", "/openapi.json"}

    @staticmethod
    def _get_client_ip(request: Request) -> str:
        """Extract real client IP, respecting X-Forwarded-For from trusted proxies."""
        settings = get_settings()
        direct_ip = request.client.host if request.client else "unknown"
        if direct_ip in settings.TRUSTED_PROXIES:
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return direct_ip

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in self.SKIP_PATHS:
            return await call_next(request)

        # Skip WebSocket upgrades
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        try:
            from app.core.redis import check_rate_limit
            client_ip = self._get_client_ip(request)
            allowed = await check_rate_limit(
                f"ip:{client_ip}",
                get_settings().RATE_LIMIT_PER_MINUTE,
                window=60,
            )
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded", "error_code": "RATE_LIMIT_EXCEEDED"},
                    headers={"Retry-After": "60"},
                )
        except Exception:
            # If Redis is down, allow the request (graceful degradation)
            pass

        return await call_next(request)


class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    """Catch unhandled exceptions and return structured error responses."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await call_next(request)
        except AppException:
            raise  # Let FastAPI handle these
        except Exception as exc:
            request_id = getattr(request.state, "request_id", "unknown")
            settings = get_settings()

            # Forward upstream HTTP errors (Cashfree, Razorpay, etc.) to client
            import httpx
            if isinstance(exc, httpx.HTTPStatusError):
                upstream_status = exc.response.status_code
                try:
                    body = exc.response.json()
                except Exception:
                    body = exc.response.text
                logger.warning(
                    "upstream_api_error",
                    request_id=request_id,
                    path=request.url.path,
                    upstream_url=str(exc.request.url),
                    upstream_status=upstream_status,
                    upstream_body=body,
                )
                return JSONResponse(
                    status_code=upstream_status if 400 <= upstream_status < 500 else 502,
                    content={
                        "detail": "Upstream API error",
                        "error_code": "UPSTREAM_ERROR",
                        "request_id": request_id,
                    },
                )
            logger.exception(
                "unhandled_exception",
                request_id=request_id,
                path=request.url.path,
                exc_type=type(exc).__name__,
            )
            detail = str(exc) if settings.is_development else "Internal server error"
            return JSONResponse(
                status_code=500,
                content={
                    "detail": detail,
                    "error_code": "INTERNAL_ERROR",
                    "request_id": request_id,
                },
            )


class SubscriptionCheckMiddleware(BaseHTTPMiddleware):
    """
    Check active subscription before allowing access to protected API routes.
    Skips public endpoints (auth, health, plans, webhooks, docs).
    Returns 403 with subscription_required error if no active subscription.
    """

    # Paths that don't require an active subscription
    EXEMPT_PREFIXES = (
        "/health", "/metrics", "/docs", "/openapi.json", "/redoc",
        "/ws",
        "/api/v1/auth",
        "/api/v1/webhooks",
        "/api/v1/tables/qr",
        "/api/v1/subscriptions/plans",
        "/api/v1/subscriptions/addons",
        "/api/v1/subscriptions/free-trial",
        "/api/v1/subscriptions/subscribe",
        "/api/v1/subscriptions/verify",
        "/api/v1/subscriptions/status",
        "/api/v1/subscriptions/cancel",
        "/api/v1/subscriptions/upgrade",
        "/api/v1/subscriptions/downgrade",
        "/api/v1/subscriptions/addons/purchase",
        "/api/v1/health",
    )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Skip exempt paths
        if any(path.startswith(prefix) for prefix in self.EXEMPT_PREFIXES):
            return await call_next(request)

        # Skip non-API paths
        if not path.startswith("/api/"):
            return await call_next(request)

        # Skip OPTIONS (CORS preflight)
        if request.method == "OPTIONS":
            return await call_next(request)

        # Extract user_id from JWT (lightweight check — don't block if no token)
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            # No auth = let the auth dependency handle the 401
            return await call_next(request)

        try:
            from app.core.auth import decode_jwt
            token = auth_header.split(" ", 1)[1]
            payload = decode_jwt(token)
            user_id = payload.get("sub", "")
        except Exception:
            return await call_next(request)

        if not user_id:
            return await call_next(request)

        # Check subscription
        try:
            from app.services.subscription_service import SubscriptionService
            svc = SubscriptionService()
            is_active = await svc.check_active(user_id)
        except Exception:
            # If check fails, allow request (graceful degradation)
            return await call_next(request)

        if not is_active:
            logger.warning(
                "subscription_blocked",
                user_id=user_id,
                path=path,
                is_active=is_active,
            )
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "Active subscription required. Please subscribe to continue.",
                    "error_code": "SUBSCRIPTION_REQUIRED",
                    "subscribe_url": "/pricing",
                },
            )

        return await call_next(request)
