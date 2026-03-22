"""
Structured logging with structlog.
All log entries are JSON-structured for aggregation in production.
Console-rendered in development for readability.
"""
import logging
import os
import sys
import structlog
from app.core.config import get_settings


def setup_logging():
    settings = get_settings()
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if settings.is_production:
        # Production: JSON, add service metadata
        shared_processors.append(structlog.processors.EventRenamer("message"))
        shared_processors.append(structlog.processors.JSONRenderer())
    else:
        shared_processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=shared_processors,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    root_logger.addHandler(handler)

    # Suppress noisy libraries
    for lib in ("uvicorn.access", "asyncpg", "httpx", "httpcore", "hpack"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def get_logger(name: str = __name__) -> structlog.BoundLogger:
    return structlog.get_logger(name)
