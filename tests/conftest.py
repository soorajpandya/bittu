"""
Test fixtures and configuration for BITTU backend tests.
"""
import os
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

# Force test environment
os.environ["APP_ENV"] = "testing"
os.environ["ENVIRONMENT"] = "testing"
os.environ["DEBUG"] = "false"


@pytest.fixture(scope="session")
def event_loop():
    """Create an event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_db_connection():
    """Mock asyncpg connection for unit tests."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    return conn


@pytest.fixture
def mock_redis():
    """Mock Redis client for unit tests."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock()
    redis.delete = AsyncMock()
    redis.incr = AsyncMock(return_value=1)
    redis.expire = AsyncMock()
    redis.pipeline = MagicMock()
    return redis


@pytest.fixture
def owner_context():
    """Sample owner UserContext for testing."""
    from app.core.auth import UserContext
    return UserContext(
        user_id="test-owner-id",
        email="owner@test.com",
        role="owner",
        restaurant_id="test-restaurant-id",
        branch_id="test-branch-id",
        owner_id="test-owner-id",
        is_branch_user=False,
    )


@pytest.fixture
def manager_context():
    """Sample manager UserContext for testing."""
    from app.core.auth import UserContext
    return UserContext(
        user_id="test-manager-id",
        email="manager@test.com",
        role="manager",
        restaurant_id="test-restaurant-id",
        branch_id="test-branch-id",
        owner_id="test-owner-id",
        is_branch_user=True,
    )


@pytest.fixture
def cashier_context():
    """Sample cashier UserContext for testing."""
    from app.core.auth import UserContext
    return UserContext(
        user_id="test-cashier-id",
        email="cashier@test.com",
        role="cashier",
        restaurant_id="test-restaurant-id",
        branch_id="test-branch-id",
        owner_id="test-owner-id",
        is_branch_user=True,
    )
