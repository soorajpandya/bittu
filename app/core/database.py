"""
Database connection management with connection pooling.
Uses asyncpg with SQLAlchemy async engine for production-grade pooling.
"""
import asyncpg
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from app.core.config import get_settings


def _settings():
    return get_settings()


# ── Async SQLAlchemy Engine (lazy init) ──
_engine = None
_async_session_factory = None


def _get_engine():
    global _engine, _async_session_factory
    if _engine is None:
        settings = _settings()
        _engine = create_async_engine(
            settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://"),
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_timeout=settings.DB_POOL_TIMEOUT,
            pool_recycle=settings.DB_POOL_RECYCLE,
            pool_pre_ping=True,
            echo=settings.DEBUG,
            connect_args={
                "statement_cache_size": 0,
                "command_timeout": settings.DB_STATEMENT_TIMEOUT / 1000,
            },
        )
        _async_session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    return _engine

# ── Raw asyncpg pool (for high-perf raw SQL) ──
_pool: asyncpg.Pool | None = None


async def init_db_pool():
    """Initialize the raw asyncpg connection pool."""
    global _pool
    settings = _settings()
    _pool = await asyncpg.create_pool(
        dsn=settings.DATABASE_URL,
        min_size=5,
        max_size=settings.DB_POOL_SIZE,
        max_inactive_connection_lifetime=settings.DB_POOL_RECYCLE,
        command_timeout=settings.DB_STATEMENT_TIMEOUT / 1000,
        statement_cache_size=0,
        server_settings={
            'tcp_keepalives_idle': '60',
            'tcp_keepalives_interval': '10',
            'tcp_keepalives_count': '3',
        },
    )


async def close_db_pool():
    """Gracefully close all database connections."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
    engine = _get_engine()
    if engine:
        await engine.dispose()


def get_pool() -> asyncpg.Pool:
    """Get the raw asyncpg pool. Raises if not initialized."""
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_db_pool() first.")
    return _pool


@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency: yields an async SQLAlchemy session."""
    _get_engine()  # ensure engine is initialized
    async with _async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_connection() -> AsyncGenerator[asyncpg.Connection, None]:
    """Dependency: yields a raw asyncpg connection from pool."""
    pool = get_pool()
    async with pool.acquire() as conn:
        yield conn


@asynccontextmanager
async def get_transaction() -> AsyncGenerator[asyncpg.Connection, None]:
    """Dependency: yields a connection inside a transaction."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            yield conn


@asynccontextmanager
async def get_serializable_transaction() -> AsyncGenerator[asyncpg.Connection, None]:
    """
    SERIALIZABLE isolation transaction for critical operations
    (payments, inventory deductions, order state changes).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction(isolation="serializable"):
            yield conn
