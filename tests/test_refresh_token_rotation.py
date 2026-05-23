"""
Tests for refresh-token rotation + reuse detection.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.services import refresh_token_service as rts_module
from app.services.refresh_token_service import (
    ReuseDetected,
    RefreshTokenService,
    _hash,
)

USER_ID = "00000000-0000-0000-0000-0000000000aa"
DEVICE_ID = "device-xyz"


def _conn_cm(conn):
    @asynccontextmanager
    async def _cm():
        yield conn
    return _cm()


@pytest.mark.asyncio
async def test_record_issuance_inserts_and_revokes_parent():
    conn = AsyncMock()
    conn.execute = AsyncMock()
    with patch.object(rts_module, "get_service_connection", lambda: _conn_cm(conn)):
        svc = RefreshTokenService()
        await svc.record_issuance(
            user_id=USER_ID,
            device_id=DEVICE_ID,
            token="new-token",
            parent_token="old-token",
        )
    assert conn.execute.await_count == 2
    insert_sql = conn.execute.await_args_list[0].args[0]
    update_sql = conn.execute.await_args_list[1].args[0]
    assert "INSERT INTO refresh_tokens" in insert_sql
    assert "rotated_to" in update_sql
    # parent hash should be the sha256 of the old token
    assert _hash("old-token") in conn.execute.await_args_list[1].args


@pytest.mark.asyncio
async def test_check_for_reuse_unknown_token_passes():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    with patch.object(rts_module, "get_service_connection", lambda: _conn_cm(conn)):
        svc = RefreshTokenService()
        await svc.check_for_reuse(token="unknown")  # no exception


@pytest.mark.asyncio
async def test_check_for_reuse_active_token_updates_last_seen():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "user_id": USER_ID,
        "device_id": DEVICE_ID,
        "revoked_at": None,
        "revoked_reason": None,
    })
    conn.execute = AsyncMock()
    with patch.object(rts_module, "get_service_connection", lambda: _conn_cm(conn)):
        svc = RefreshTokenService()
        await svc.check_for_reuse(token="still-good")
    assert conn.execute.await_count == 1
    assert "last_seen_at" in conn.execute.await_args_list[0].args[0]


@pytest.mark.asyncio
async def test_check_for_reuse_revoked_token_raises_and_kills_chain():
    from datetime import datetime, timezone
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "user_id": USER_ID,
        "device_id": DEVICE_ID,
        "revoked_at": datetime.now(timezone.utc),
        "revoked_reason": "rotated",
    })
    conn.fetchval = AsyncMock(return_value=3)
    conn.execute = AsyncMock()
    revoke_key = AsyncMock()
    with patch.object(rts_module, "get_service_connection", lambda: _conn_cm(conn)), \
         patch.object(rts_module, "revoke_session_key", revoke_key):
        svc = RefreshTokenService()
        with pytest.raises(ReuseDetected) as exc_info:
            await svc.check_for_reuse(token="replayed")
    assert exc_info.value.user_id == USER_ID
    assert exc_info.value.device_id == DEVICE_ID
    conn.fetchval.assert_awaited_once()
    revoke_key.assert_awaited_once_with(USER_ID, DEVICE_ID)


@pytest.mark.asyncio
async def test_revoke_for_logout_marks_token():
    conn = AsyncMock()
    conn.execute = AsyncMock()
    with patch.object(rts_module, "get_service_connection", lambda: _conn_cm(conn)):
        svc = RefreshTokenService()
        await svc.revoke_for_logout(token="abc")
    sql = conn.execute.await_args_list[0].args[0]
    assert "revoked_reason" in sql and "logout" in sql
