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


# ── Per-request tenant context (binds RLS migration 049) ──────────────────────
# A contextvar lets us push the current tenant id from FastAPI middleware
# (or directly from a service call) without changing every call signature.
# `get_connection()` reads it and pushes it into the Postgres session via
# `set_config('app.tenant_id', $1, true)` so RLS policies on user_id-owning
# tables silently filter to that tenant. NULL/empty value = NO-OP (matches
# the policy in 049, which allows access when the GUC is unset — used by
# workers and platform-admin paths).
import contextvars

_current_tenant_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "bittu_current_tenant_id", default=None
)
_bypass_rls: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "bittu_bypass_rls", default=False
)


def set_tenant_context(tenant_id: str | None) -> contextvars.Token:
    """Push a tenant id onto the current async context. Returns a token to reset."""
    return _current_tenant_id.set(str(tenant_id) if tenant_id else None)


def reset_tenant_context(token: contextvars.Token) -> None:
    _current_tenant_id.reset(token)


def get_current_tenant_id() -> str | None:
    return _current_tenant_id.get()


@asynccontextmanager
async def use_tenant(tenant_id: str | None):
    """`async with use_tenant(uid):` — scoped tenant context."""
    token = set_tenant_context(tenant_id)
    try:
        yield
    finally:
        reset_tenant_context(token)


@asynccontextmanager
async def bypass_rls():
    """Service-role scope: workers / platform-admin / cross-merchant readers.

    The 049 policy treats unset `app.tenant_id` as a wildcard, so bypassing
    is just clearing the GUC for the duration of this connection.
    """
    token = _bypass_rls.set(True)
    try:
        yield
    finally:
        _bypass_rls.reset(token)


async def _apply_tenant_guc(conn: asyncpg.Connection) -> None:
    # `app.rls_bypass = 'on'` is the explicit opt-in checked by the strict
    # `fn_rls_owner_match` (migration 071). Without it, an unset / empty
    # `app.tenant_id` will deny access on RLS-protected tables — fail-closed.
    if _bypass_rls.get():
        await conn.execute("SELECT set_config('app.tenant_id', '', true)")
        await conn.execute("SELECT set_config('app.rls_bypass', 'on', true)")
        return
    await conn.execute("SELECT set_config('app.rls_bypass', '', true)")
    tid = _current_tenant_id.get()
    if tid:
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tid)
    else:
        await conn.execute("SELECT set_config('app.tenant_id', '', true)")


@asynccontextmanager
async def get_connection() -> AsyncGenerator[asyncpg.Connection, None]:
    """Dependency: yields a raw asyncpg connection from pool, RLS-stamped."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await _apply_tenant_guc(conn)
        yield conn


@asynccontextmanager
async def get_transaction() -> AsyncGenerator[asyncpg.Connection, None]:
    """Dependency: yields a connection inside a transaction, RLS-stamped."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await _apply_tenant_guc(conn)
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
        await _apply_tenant_guc(conn)
        async with conn.transaction(isolation="serializable"):
            yield conn


@asynccontextmanager
async def get_service_connection() -> AsyncGenerator[asyncpg.Connection, None]:
    """Worker / cross-merchant connection that bypasses RLS for the call."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with bypass_rls():
            await _apply_tenant_guc(conn)
            yield conn
