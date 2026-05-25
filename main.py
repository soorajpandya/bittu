"""
BITTU — Real-time Restaurant Operating System
FastAPI application entry point.
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

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
    DeprecationHeaderMiddleware,
    RequestSecurityMiddleware,
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

    # Register ERP event handlers (inventory deduction, accounting entries)
    from app.services.erp_event_handlers import register_erp_handlers
    register_erp_handlers()

    # Waitlist web-push handlers (customer browser notifications)
    try:
        from app.services.push_service import register_push_handlers
        register_push_handlers()
    except Exception as exc:
        logger.error("push_handlers_register_failed", error=str(exc))

    # Register inventory → accounting bridge (Section 5)
    from app.services.inventory_accounting_handlers import (
        register_inventory_accounting_handlers,
    )
    register_inventory_accounting_handlers()

    # Auto-cancel orders when their payment intent dies (cancel / expire).
    # Keeps orders.status in sync with payments.status so revenue / order-count
    # / AOV aggregations stay honest.
    from app.services.order_status_handlers import register_order_status_handlers
    register_order_status_handlers()

    # Inventory snapshot scheduler (Section 9)
    snapshot_task = None
    try:
        from app.services.inventory_snapshot_scheduler import (
            start_inventory_snapshot_scheduler,
        )
        snapshot_task = start_inventory_snapshot_scheduler()
    except Exception as exc:
        logger.error("inventory_snapshot_scheduler_start_failed", error=str(exc))

    # Razorpay QR cleanup scheduler (Phase 2 deep integration)
    rzp_qr_cleanup_task = None
    try:
        from app.services.razorpay.qr_cleanup_scheduler import (
            start_rzp_qr_cleanup_scheduler,
        )
        rzp_qr_cleanup_task = start_rzp_qr_cleanup_scheduler()
    except Exception as exc:
        logger.error("rzp_qr_cleanup_scheduler_start_failed", error=str(exc))

    # Razorpay dispute polling scheduler (Phase 5 deep integration)
    rzp_dispute_polling_task = None
    try:
        from app.services.razorpay.dispute_polling_scheduler import (
            start_rzp_dispute_polling_scheduler,
        )
        rzp_dispute_polling_task = start_rzp_dispute_polling_scheduler()
    except Exception as exc:
        logger.error("rzp_dispute_polling_scheduler_start_failed", error=str(exc))

    # Razorpay settlement polling scheduler (Phase 6 deep integration)
    rzp_settlement_polling_task = None
    try:
        from app.services.razorpay.settlement_polling_scheduler import (
            start_rzp_settlement_polling_scheduler,
        )
        rzp_settlement_polling_task = start_rzp_settlement_polling_scheduler()
    except Exception as exc:
        logger.error("rzp_settlement_polling_scheduler_start_failed", error=str(exc))

    # Razorpay route polling scheduler (Phase 7 deep integration)
    rzp_route_polling_task = None
    try:
        from app.services.razorpay.route_polling_scheduler import (
            start_rzp_route_polling_scheduler,
        )
        rzp_route_polling_task = start_rzp_route_polling_scheduler()
    except Exception as exc:
        logger.error("rzp_route_polling_scheduler_start_failed", error=str(exc))

    # Razorpay smart collect polling scheduler (Phase 8 deep integration)
    rzp_smart_collect_polling_task = None
    try:
        from app.services.razorpay.smart_collect_polling_scheduler import (
            start_rzp_smart_collect_polling_scheduler,
        )
        rzp_smart_collect_polling_task = start_rzp_smart_collect_polling_scheduler()
    except Exception as exc:
        logger.error("rzp_smart_collect_polling_scheduler_start_failed", error=str(exc))

    # Razorpay invoice polling scheduler (Phase 9 deep integration)
    rzp_invoice_polling_task = None
    try:
        from app.services.razorpay.invoice_polling_scheduler import (
            start_rzp_invoice_polling_scheduler,
        )
        rzp_invoice_polling_task = start_rzp_invoice_polling_scheduler()
    except Exception as exc:
        logger.error("rzp_invoice_polling_scheduler_start_failed", error=str(exc))

    # Razorpay reconciliation scheduler (Phase 9 — daily 3-way matcher)
    rzp_reconciliation_task = None
    try:
        from app.services.razorpay.reconciliation_scheduler import (
            start_rzp_reconciliation_scheduler,
        )
        rzp_reconciliation_task = start_rzp_reconciliation_scheduler()
    except Exception as exc:
        logger.error("rzp_reconciliation_scheduler_start_failed", error=str(exc))

    logger.info("startup_complete", db=db_ok, redis=redis_ok)
    yield

    # --- shutdown ---
    if snapshot_task is not None:
        snapshot_task.cancel()
        try:
            await snapshot_task
        except asyncio.CancelledError:
            pass

    if rzp_qr_cleanup_task is not None:
        rzp_qr_cleanup_task.cancel()
        try:
            await rzp_qr_cleanup_task
        except asyncio.CancelledError:
            pass

    if rzp_dispute_polling_task is not None:
        rzp_dispute_polling_task.cancel()
        try:
            await rzp_dispute_polling_task
        except asyncio.CancelledError:
            pass

    if rzp_settlement_polling_task is not None:
        rzp_settlement_polling_task.cancel()
        try:
            await rzp_settlement_polling_task
        except asyncio.CancelledError:
            pass

    if rzp_route_polling_task is not None:
        rzp_route_polling_task.cancel()
        try:
            await rzp_route_polling_task
        except asyncio.CancelledError:
            pass

    if rzp_smart_collect_polling_task is not None:
        rzp_smart_collect_polling_task.cancel()
        try:
            await rzp_smart_collect_polling_task
        except asyncio.CancelledError:
            pass

    if rzp_invoice_polling_task is not None:
        rzp_invoice_polling_task.cancel()
        try:
            await rzp_invoice_polling_task
        except asyncio.CancelledError:
            pass

    if rzp_reconciliation_task is not None:
        rzp_reconciliation_task.cancel()
        try:
            await rzp_reconciliation_task
        except asyncio.CancelledError:
            pass

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
        from app.services.razorpay.client import shutdown_razorpay_client
        await shutdown_razorpay_client()
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

    docs_url = None
    redoc_url = None
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
    app.add_middleware(DeprecationHeaderMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware)
    # HMAC signature verification — must run AFTER RequestId (so failures
    # carry a request_id) and BEFORE any business handler. Mode is governed
    # by REQUEST_SIGNING_MODE (off / monitor / enforce).
    app.add_middleware(RequestSecurityMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(RequestIdMiddleware)

    # -- CORS (outermost — added last so it wraps everything) --
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Request-ID",
            "X-Idempotency-Key",
            # HMAC request signing headers (RequestSecurityMiddleware)
            "X-Device-Id",
            "X-Timestamp",
            "X-Nonce",
            "X-Signature",
        ],
    )

    # -- Structured exception handlers --
    # All error responses follow the shape:
    #   {"error": {"code": "...", "message": "...", "details": {}, "retryable": bool}}
    # This is consistent across AppException (business errors), request
    # validation failures, and unhandled server errors.

    from fastapi import Request as _Req
    from fastapi.responses import JSONResponse as _JSONResp
    from fastapi.exceptions import RequestValidationError as _RVE
    from app.core.exceptions import AppException as _AppEx

    @app.exception_handler(_AppEx)
    async def app_exception_handler(request: _Req, exc: _AppEx) -> _JSONResp:
        request_id = getattr(request.state, "request_id", None)
        return _JSONResp(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.error_code,
                    "message": exc.detail,
                    "details": {},
                    "retryable": getattr(exc, "retryable", False),
                },
                "request_id": request_id,
            },
        )

    @app.exception_handler(_RVE)
    async def validation_exception_handler(request: _Req, exc: _RVE) -> _JSONResp:
        request_id = getattr(request.state, "request_id", None)
        # Pydantic v2 stuffs the raw exception (e.g. ValueError) into
        # ``errors[].ctx.error``. That object is not JSON-serializable, so
        # JSONResponse.render() raises ``TypeError: Object of type ValueError
        # is not JSON serializable`` and the merchant sees a 500 instead of a
        # 422. Sanitize with jsonable_encoder so every value is a primitive.
        from fastapi.encoders import jsonable_encoder as _je
        try:
            _raw_errors = exc.errors()
        except Exception:
            _raw_errors = []
        try:
            _safe_errors = _je(_raw_errors, custom_encoder={Exception: lambda e: str(e)})
        except Exception:
            _safe_errors = [{"msg": "validation failed", "type": "unknown"}]
        try:
            from app.core.logging import get_logger as _get_logger
            _vlog = _get_logger("app.validation")
            _vlog.warning(
                "request_validation_failed",
                extra={
                    "path": str(request.url.path),
                    "method": request.method,
                    "request_id": request_id,
                    "errors": _safe_errors,
                    "body_preview": (str(exc.body)[:500] if getattr(exc, "body", None) is not None else None),
                },
            )
        except Exception:
            pass
        return _JSONResp(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed",
                    "details": {"errors": _safe_errors},
                    "retryable": False,
                },
                "request_id": request_id,
            },
        )

    # -- Razorpay upstream errors → structured, user-actionable responses --
    # Without this, RazorpayAPIError falls through to the generic 500
    # INTERNAL_ERROR handler in the middleware which lies about retryability
    # (admin-locked KYC forms, for example, are NOT retryable).
    from app.services.razorpay.client import (
        RazorpayAPIError as _RzpAPIErr,
        RazorpayBadRequestError as _RzpBadReq,
        RazorpayRateLimitedError as _RzpRateLimited,
        RazorpayServerError as _RzpServerErr,
    )

    def _map_razorpay_error(exc: _RzpAPIErr) -> tuple[int, str, str, bool]:
        """Return (http_status, error_code, user_message, retryable)."""
        desc = (exc.error_description or "").strip()
        rzp_code = (exc.error_code or "").strip()
        lower = desc.lower()

        # Razorpay locks the merchant activation form while their ops team
        # reviews a needs_clarification submission. No edits possible until
        # they release it; retrying just hammers the same 400.
        if "locked for editing" in lower or "form has been locked" in lower:
            return (
                423,
                "RZP_FORM_LOCKED",
                (
                    "Razorpay is currently reviewing your account. "
                    "Please wait for their response or contact Razorpay "
                    "support to release the activation form before retrying."
                ),
                False,
            )
        if isinstance(exc, _RzpRateLimited):
            return (429, "RZP_RATE_LIMITED", desc or "Razorpay rate limit hit.", True)
        if isinstance(exc, _RzpServerErr):
            return (
                502,
                "RZP_UPSTREAM_ERROR",
                desc or "Razorpay is temporarily unavailable.",
                True,
            )
        if isinstance(exc, _RzpBadReq):
            # Surface Razorpay's own description verbatim — it's already
            # the most useful thing we can show the merchant.
            msg = desc or "Razorpay rejected the request."
            if rzp_code:
                msg = f"{msg} ({rzp_code})"
            return (422, "RZP_BAD_REQUEST", msg, False)
        # Unknown upstream shape.
        return (
            502,
            "RZP_UPSTREAM_ERROR",
            desc or "Razorpay returned an unexpected error.",
            False,
        )

    @app.exception_handler(_RzpAPIErr)
    async def razorpay_exception_handler(request: _Req, exc: _RzpAPIErr) -> _JSONResp:
        request_id = getattr(request.state, "request_id", None)
        status_code, code, message, retryable = _map_razorpay_error(exc)
        try:
            from app.core.logging import get_logger as _get_logger
            _rlog = _get_logger("app.razorpay")
            _rlog.warning(
                "razorpay_upstream_error",
                extra={
                    "path": str(request.url.path),
                    "method": request.method,
                    "request_id": request_id,
                    "rzp_status": exc.status_code,
                    "rzp_error_code": exc.error_code,
                    "rzp_description": exc.error_description,
                    "mapped_status": status_code,
                    "mapped_code": code,
                },
            )
        except Exception:
            pass
        return _JSONResp(
            status_code=status_code,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "details": {
                        "rzp_status": exc.status_code,
                        "rzp_error_code": exc.error_code,
                        "rzp_description": exc.error_description,
                    },
                    "retryable": retryable,
                },
                "request_id": request_id,
            },
        )

    # -- Routes --
    app.include_router(api_router)

    # -- Short, branded customer-facing URL for QR waitlist landing --
    # Printed QR codes encode https://<host>/q/<restaurant_id> which is
    # short, clean, and keeps the API surface out of the customer's URL bar.
    from app.api.v1.waitlist import qr_landing_page as _qr_landing
    from uuid import UUID as _UUID

    @app.get("/q/{restaurant_id}", include_in_schema=False)
    async def _qr_short(restaurant_id: _UUID):
        return await _qr_landing(restaurant_id)

    # -- Pin OpenAPI version to 3.0.3 --
    # FastAPI emits 3.1.0 by default, which the bundled Swagger UI 4.15.5
    # cannot render ("does not specify a valid version field"). Re-stamp
    # the cached schema so /docs and /redoc work without a CDN upgrade.
    from fastapi.openapi.utils import get_openapi as _get_openapi

    def _custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = _get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        schema["openapi"] = "3.0.3"
        app.openapi_schema = schema
        return schema

    app.openapi = _custom_openapi

    # -- Self-hosted Docs (no CDN dependency) --
    import swagger_ui_bundle, os as _os
    _swagger_static = _os.path.join(_os.path.dirname(swagger_ui_bundle.__file__), "vendor", "swagger-ui-4.15.5")
    app.mount("/_swagger-ui", StaticFiles(directory=_swagger_static), name="swagger_ui_static")

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title="BITTU API - Swagger UI",
            swagger_js_url="/_swagger-ui/swagger-ui-bundle.js",
            swagger_css_url="/_swagger-ui/swagger-ui.css",
            swagger_favicon_url="/_swagger-ui/favicon-32x32.png",
        )

    @app.get("/redoc", include_in_schema=False)
    async def redoc_ui() -> HTMLResponse:
        return get_redoc_html(
            openapi_url="/openapi.json",
            title="BITTU API - ReDoc",
            redoc_js_url="https://cdn.jsdelivr.net/npm/redoc@latest/bundles/redoc.standalone.js",
            redoc_favicon_url="/_swagger-ui/favicon-32x32.png",
            with_google_fonts=False,
        )

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
