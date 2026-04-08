"""
BITTU — Real-time Restaurant Operating System
FastAPI application entry point.
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.database import init_db_pool, close_db_pool
from app.core.redis import init_redis, close_redis
from app.core.logging import setup_logging, get_logger
from app.middleware import (
    RequestIdMiddleware,
    RequestLoggingMiddleware,
    RateLimitMiddleware,
    ErrorHandlerMiddleware,
    SecurityHeadersMiddleware,
    SubscriptionCheckMiddleware,
)
from app.api import router as api_router
from app.realtime import ws_endpoint, ws_session_endpoint, redis_subscriber
from app.core.metrics import metrics_endpoint

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────
# Lifespan (startup / shutdown)
# ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging()

    logger.info("starting", env=settings.ENVIRONMENT)

    # --- startup (graceful — server starts even if backing services are down) ---
    db_ok = False
    redis_ok = False
    subscriber_task = None

    try:
        await init_db_pool()
        db_ok = True
    except Exception as exc:
        logger.error("db_connect_failed", error=str(exc))

    try:
        await init_redis()
        redis_ok = True
    except Exception as exc:
        logger.error("redis_connect_failed", error=str(exc))

    if redis_ok:
        subscriber_task = asyncio.create_task(redis_subscriber())

    # Register accounting sync handlers (bridge restaurant → accounting)
    from app.services.accounting_sync_service import register_accounting_handlers
    register_accounting_handlers()

    logger.info("startup_complete", db=db_ok, redis=redis_ok)
    yield

    # --- shutdown ---
    if subscriber_task is not None:
        subscriber_task.cancel()
        try:
            await subscriber_task
        except asyncio.CancelledError:
            pass

    try:
        await close_redis()
    except Exception:
        pass
    try:
        await close_db_pool()
    except Exception:
        pass
    logger.info("shutdown_complete")


# ──────────────────────────────────────────────────────────────
# Application factory
# ──────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    settings = get_settings()

    docs_url = "/docs"
    redoc_url = "/redoc"
    openapi_url = "/openapi.json"

    app = FastAPI(
        title="BITTU API",
        version="1.0.0",
        description="Real-time Restaurant Operating System",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )

    # -- Custom middleware (outermost → innermost) --
    # NOTE: add_middleware wraps in reverse — last added = outermost.
    # CORSMiddleware must be outermost so CORS headers are present
    # even on error responses from ErrorHandlerMiddleware.
    app.add_middleware(ErrorHandlerMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    # app.add_middleware(SubscriptionCheckMiddleware)  # Disabled — no subscription check
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(RequestIdMiddleware)

    # -- CORS (outermost — added last so it wraps everything) --
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Idempotency-Key"],
    )

    # -- Routes --
    app.include_router(api_router)

    # -- Prometheus metrics (unauthenticated, for scraping) --
    app.add_route("/metrics", metrics_endpoint, methods=["GET"])

    # -- WebSocket --
    from fastapi import WebSocket, Query as WSQuery

    @app.websocket("/ws")
    async def websocket_route(
        websocket: WebSocket,
        token: str = WSQuery(default=""),
    ):
        await ws_endpoint(websocket, token=token or None)

    # -- Public WebSocket for QR dine-in customers (session_token auth) --
    @app.websocket("/ws/session")
    async def websocket_session_route(
        websocket: WebSocket,
        session_token: str = WSQuery(default=""),
    ):
        await ws_session_endpoint(websocket, session_token=session_token or None)

    return app


app = create_app()
