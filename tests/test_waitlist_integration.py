"""Regression coverage for the nine backend/browser integration findings."""

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.applications import Starlette
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

from tests.test_waitlist_safety import (
    db,
    waitlist_postgres,
    user,
    service,
    table,
    add,
    RID,
    RID2,
    B1,
    B2,
    OWNER,
)
from app.core.auth import UserContext
from app.core.exceptions import NotFoundError, ValidationError, ConflictError


def keys():
    point = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    return {
        "p256dh": base64.urlsafe_b64encode(point).rstrip(b"=").decode(),
        "auth": base64.urlsafe_b64encode(b"a" * 16).rstrip(b"=").decode(),
    }


@pytest.mark.parametrize(
    "method,path,expected",
    [
        ("GET", f"/q/{RID}", 200),
        ("GET", f"/api/v1/waitlist/qr/{RID}", 200),
        ("GET", f"/api/v1/waitlist/qr/{RID}.png", 200),
        ("POST", f"/api/v1/waitlist/public/{RID}", 200),
        ("GET", f"/api/v1/waitlist/status/{RID}", 200),
        ("POST", f"/api/v1/waitlist/push/subscribe/{RID}", 200),
        ("GET", "/api/v1/waitlist/sw.js", 200),
        ("GET", "/api/v1/waitlist/assets/logo.png", 200),
        ("GET", "/api/v1/waitlist/push/vapid-key", 200),
        ("POST", "/api/v1/waitlist/notify-next", 401),
        ("PUT", "/api/v1/waitlist/settings", 401),
        ("POST", f"/api/v1/waitlist/{RID}/seat", 401),
        ("DELETE", f"/q/{RID}", 401),
    ],
)
async def test_signing_exempts_only_public_methods(method, path, expected):
    from app.middleware.request_security import RequestSecurityMiddleware

    async def endpoint(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route(path, endpoint, methods=[method])])
    app.add_middleware(RequestSecurityMiddleware, mode="enforce")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path)
    assert response.status_code == expected


async def test_production_landing_nonce_and_untrusted_names(db, monkeypatch):
    from app import middleware
    from app.api.v1 import waitlist
    from app.middleware.request_security import RequestSecurityMiddleware

    malicious = '";alert(1);//</script><img src=x onerror=alert(2)>'
    async with db() as conn:
        await conn.execute("UPDATE restaurants SET name=$1 WHERE id=$2::uuid", malicious, RID)
    monkeypatch.setattr(waitlist, "get_service_connection", db)
    monkeypatch.setattr(middleware, "get_settings", lambda: SimpleNamespace(is_production=True))
    app = FastAPI()
    app.include_router(waitlist.router, prefix="/api/v1")
    app.add_middleware(middleware.SecurityHeadersMiddleware)
    app.add_middleware(RequestSecurityMiddleware, mode="enforce")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        r = await client.get(f"/api/v1/waitlist/qr/{RID}")
    assert r.status_code == 200
    import re

    nonce = re.search(r'<script nonce="([^"]+)"', r.text).group(1)
    assert f"'nonce-{nonce}'" in r.headers["content-security-policy"]
    assert '<style nonce="' + nonce + '">' in r.text
    assert "<img src=x" not in r.text and 'var RNAME = "' not in r.text
    assert 'style="' not in r.text
    assert r.headers["cache-control"] == "no-store"
    assert "location.pathname.startsWith('/q/')" in r.text


async def test_seating_paid_vacate_releases_both_engines(db, user, service, monkeypatch):
    from app.services import dinein_session_service as dinein

    tid = await table(db, user)
    first = await add(service, user)
    seated = await service.seat_customer(user, first["id"], str(tid))
    async with db() as conn:
        await conn.execute("ALTER TABLE restaurant_tables ADD COLUMN current_order_id uuid")
        await conn.execute(
            "ALTER TABLE dine_in_sessions ADD COLUMN last_activity_at timestamptz, ADD COLUMN active_users_count int"
        )
        await conn.execute("CREATE TABLE table_session_users(session_id uuid,is_active bool)")
        assert await conn.fetchval(
            "SELECT legacy_session_id FROM dine_in_sessions WHERE id=$1::uuid", seated["session_id"]
        ) == UUID(seated["session_id"])

    @asynccontextmanager
    async def transaction():
        async with db() as conn, conn.transaction():
            yield conn

    monkeypatch.setattr(dinein, "get_transaction", transaction)
    monkeypatch.setattr(dinein, "emit_and_publish", AsyncMock())
    monkeypatch.setattr(dinein, "cache_delete", AsyncMock())
    svc = dinein.DineInSessionService()
    monkeypatch.setattr(svc, "get_session_bill", AsyncMock(return_value={"remaining_amount": 0}))
    await svc.paid_and_vacate(seated["session_id"])
    async with db() as conn:
        assert not await conn.fetchval(
            "SELECT is_active FROM table_sessions WHERE id=$1::uuid", seated["session_id"]
        )
        assert not await conn.fetchval("SELECT is_occupied FROM restaurant_tables WHERE id=$1", tid)
    replay = await service.seat_customer(user, first["id"])
    assert "session_token" not in replay
    await add(service, user)
    assert (await service.notify_next(user))["assigned_table_id"] == str(tid)


async def test_legacy_close_closes_only_original_dinein_visit(db, user, service):
    tid = await table(db, user)
    seated = await service.seat_customer(user, (await add(service, user))["id"], str(tid))
    async with db() as conn:
        later = await conn.fetchval(
            "INSERT INTO dine_in_sessions(table_id,status,session_token) VALUES($1,'active','different-visit') RETURNING id",
            tid,
        )
        await conn.execute(
            "UPDATE table_sessions SET is_active=false WHERE id=$1::uuid", seated["session_id"]
        )
        assert (
            await conn.fetchval("SELECT status FROM dine_in_sessions WHERE id=$1::uuid", seated["session_id"])
            == "completed"
        )
        assert await conn.fetchval("SELECT status FROM dine_in_sessions WHERE id=$1", later) == "active"


async def test_outbox_retry_claim_and_terminal_cancellation(db, user, service, monkeypatch):
    from app.services import push_service as push

    await table(db, user)
    entry = await add(service, user)
    await service.notify_next(user)
    sender = AsyncMock(side_effect=RuntimeError("provider down"))
    monkeypatch.setattr(push, "send_push", sender)
    await asyncio.gather(push.run_delivery_tick(), push.run_delivery_tick())
    assert sender.await_count == 1
    async with db() as conn:
        job = await conn.fetchrow("SELECT * FROM waitlist_push_jobs WHERE entry_id=$1::uuid", entry["id"])
        assert job["state"] == "pending" and job["attempts"] == 1
        await conn.execute("UPDATE waitlist_push_jobs SET next_attempt_at=now()")
    sender.side_effect = None
    sender.return_value = 1
    await push.run_delivery_tick()
    assert sender.await_count == 2
    assert sender.call_args.args[1]["url"].endswith("entry_id=" + entry["id"])
    assert (await service.get_entry_status(entry["id"]))["delivery_status"] == "delivered"
    await service.cancel_entry(user, entry["id"])
    await push.run_delivery_tick()
    assert sender.await_count == 2


async def test_crashed_worker_lease_reclaimed_and_expiry_not_sent(db, user, service, monkeypatch):
    from app.services import push_service as push

    await table(db, user)
    entry = await add(service, user)
    await service.notify_next(user)
    async with db() as conn:
        await conn.execute(
            "UPDATE waitlist_push_jobs SET state='sending',lease_until=now()-interval '1 second',lease_token=$1",
            uuid4(),
        )
    sender = AsyncMock(return_value=1)
    monkeypatch.setattr(push, "send_push", sender)
    await push.run_delivery_tick()
    assert sender.await_count == 1
    async with db() as conn:
        await conn.execute("UPDATE waitlist_push_jobs SET state='pending',next_attempt_at=now()")
        await conn.execute(
            "UPDATE waitlist_entries SET expires_at=now()-interval '1 second' WHERE id=$1::uuid", entry["id"]
        )
    await push.run_delivery_tick()
    assert sender.await_count == 1
    assert (await service.get_entry_status(entry["id"]))["delivery_status"] == "cancelled"


async def test_subscription_proof_cap_and_late_retry(db, user, service, monkeypatch):
    from app.api.v1 import waitlist
    from app.services import push_service as push

    entry = await service.add_entry_public(RID, "Guest", 2, "9876543210")
    await table(db, user)
    await service.notify_next(user)
    monkeypatch.setattr(waitlist, "get_service_connection", db)
    app = FastAPI()
    app.include_router(waitlist.router)
    payload = {"endpoint": "https://fcm.googleapis.com/fcm/send/one", "keys": keys(), "push_token": "x" * 43}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/waitlist/push/subscribe/" + entry["id"], json=payload)).status_code == 403
        payload["push_token"] = entry["push_token"]
        assert (await client.post("/waitlist/push/subscribe/" + entry["id"], json=payload)).status_code == 200
    async with db() as conn:
        await conn.execute("UPDATE waitlist_push_jobs SET state='failed',attempts=5")
    k = keys()
    for endpoint in ("two", "three"):
        await push.save_subscription(entry["id"], RID, "https://fcm.googleapis.com/" + endpoint, **k)
    with pytest.raises(ConflictError):
        await push.save_subscription(entry["id"], RID, "https://fcm.googleapis.com/four", **k)
    async with db() as conn:
        assert await conn.fetchval("SELECT state FROM waitlist_push_jobs") == "pending"
    status = await service.get_entry_status(entry["id"])
    assert "push_token" not in status and "push_token_hash" not in status


@pytest.mark.parametrize(
    "url",
    [
        "http://fcm.googleapis.com/x",
        "https://127.0.0.1/x",
        "https://evil.test/x",
        "https://fcm.googleapis.com.evil.test/x",
        "https://user@fcm.googleapis.com/x",
        "https://fcm.googleapis.com:8443/x",
    ],
)
def test_push_rejects_unsafe_endpoint(url):
    from app.services.push_service import validate_subscription

    with pytest.raises(ValueError):
        validate_subscription(url, **keys())


def test_push_rejects_invalid_curve_point():
    from app.services.push_service import validate_subscription

    with pytest.raises(ValueError):
        validate_subscription(
            "https://fcm.googleapis.com/x",
            base64.urlsafe_b64encode(b"\x04" + b"a" * 64).decode(),
            keys()["auth"],
        )


def test_websocket_acl():
    from app.realtime import _can_subscribe

    owner = UserContext(user_id=OWNER, role="owner", restaurant_id=RID, branch_id=B1)
    staff = UserContext(
        user_id="staff", owner_id=OWNER, is_branch_user=True, role="manager", restaurant_id=RID, branch_id=B1
    )
    for ctx in (owner, staff):
        assert not _can_subscribe(ctx, "restaurant:" + RID2)
        assert not _can_subscribe(ctx, "branch:" + B2)
        assert not _can_subscribe(ctx, "entity:" + str(uuid4()))
        assert _can_subscribe(ctx, "branch:" + B1)
    assert _can_subscribe(owner, "restaurant:" + RID)
    assert not _can_subscribe(staff, "restaurant:" + RID)


async def test_realtime_waitlist_permission_filter():
    from app.realtime import ConnectionManager, WSConnection

    mgr = ConnectionManager()
    denied = WSConnection(ws=SimpleNamespace(send_json=AsyncMock()), user_id="no")
    allowed = WSConnection(ws=SimpleNamespace(send_json=AsyncMock()), user_id="yes", can_read_waitlist=True)
    for conn in (denied, allowed):
        await mgr.subscribe(conn, "branch:" + B1)
    await mgr.broadcast("branch:" + B1, {"event": "waitlist.updated", "data": {"id": "entry"}})
    denied.ws.send_json.assert_not_awaited()
    allowed.ws.send_json.assert_awaited_once()


async def test_table_scope_denies_other_location_and_session(db, user, service, monkeypatch):
    from app.services.waitlist_tables import assert_table_scope

    tid = await table(db, user)
    seated = await service.seat_customer(user, (await add(service, user))["id"], str(tid))
    other = UserContext(user_id=OWNER, restaurant_id=RID2)
    async with db() as conn:
        for target in (tid, seated["session_id"]):
            with pytest.raises(NotFoundError):
                await assert_table_scope(conn, other, target)
            assert await assert_table_scope(conn, user, target) == tid


async def test_history_local_day_and_invalid_date(db, user, service):
    a = await add(service, user)
    b = await add(service, user)
    async with db() as conn:
        await conn.execute(
            "UPDATE waitlist_entries SET created_at='2026-09-24 19:00+00' WHERE id=$1::uuid", a["id"]
        )
        await conn.execute(
            "UPDATE waitlist_entries SET created_at='2026-09-25 19:00+00' WHERE id=$1::uuid", b["id"]
        )
    history = await service.get_history(user, date_from="2026-09-25", date_to="2026-09-25")
    assert [e["id"] for e in history["entries"]] == [a["id"]]
    with pytest.raises(ValidationError):
        await service.get_history(user, date_from="not-a-date")


async def test_eta_recomputed_on_reorder_cancel_and_impossible_party(db, user, service):
    await table(db, user, capacity=4)
    a, b = await add(service, user), await add(service, user)
    impossible = await add(service, user, party=8)
    await service.reorder(user, [b["id"], a["id"], impossible["id"]])
    queue = {e["id"]: e for e in (await service.get_queue(user))["entries"]}
    assert queue[b["id"]]["estimated_wait_minutes"] == 0
    assert queue[a["id"]]["estimated_wait_minutes"] == 30
    assert queue[impossible["id"]]["estimated_wait_minutes"] is None
    await service.cancel_entry(user, b["id"])
    assert (await service.get_entry_status(a["id"]))["estimated_wait_minutes"] == 0


async def test_unavailable_channels_cannot_be_enabled(db, user, service):
    for channel in ("sms_enabled", "whatsapp_enabled"):
        with pytest.raises(ValidationError, match="unavailable"):
            await service.update_settings(user, {channel: True})
    assert (await service.get_settings(user))["delivery_capabilities"] == {
        "browser_push": True,
        "sms": False,
        "whatsapp": False,
    }


async def test_real_webpush_encryption_uses_pem_key_and_bounded_no_redirect_http(
    db, user, service, monkeypatch
):
    import requests
    from app.services import push_service as push

    await table(db, user)
    entry = await add(service, user)
    await service.notify_next(user)
    await push.save_subscription(entry["id"], RID, "https://fcm.googleapis.com/send/test", **keys())
    monkeypatch.setattr(push, "_cached_keys", push._generate_keypair())
    calls = []

    def fake_request(self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        response = requests.Response()
        response.status_code = 201
        response._content = b""
        return response

    monkeypatch.setattr(requests.Session, "request", fake_request)
    assert await push.send_push(entry["id"], {"title": "Ready"}) == 1
    assert len(calls) == 1
    assert calls[0][2]["timeout"] == 10 and calls[0][2]["allow_redirects"] is False
    assert calls[0][2]["headers"]["authorization"].startswith("vapid ")


async def test_table_list_branch_scope_and_reservation_live_state(db, user, service, monkeypatch):
    from app.api.v1 import tables

    monkeypatch.setattr(tables, "get_connection", db)
    monkeypatch.setattr(tables, "cache_set", AsyncMock())
    async with db() as conn:
        await conn.execute(
            "ALTER TABLE restaurant_tables ADD COLUMN current_order_id uuid,ADD COLUMN created_at timestamptz DEFAULT now(),ADD COLUMN updated_at timestamptz DEFAULT now()"
        )
    tid = await table(db, user)
    await table(db, UserContext(user_id=OWNER, restaurant_id=RID2))
    await add(service, user)
    await service.notify_next(user)
    rows = await tables.list_tables(user)
    assert len(rows) == 1 and rows[0]["id"] == tid and rows[0]["status"] == "reserved"
    seated = await service.seat_customer(user, (await service.get_queue(user))["entries"][0]["id"])
    rows = await tables.list_tables(user)
    assert rows[0]["active_session_id"] == UUID(seated["session_id"]) and rows[0]["status"] == "occupied"


async def test_stats_uses_local_midnight(db, user, service, monkeypatch):
    from app.services import waitlist_service as module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(module, "datetime", FixedDatetime)
    a = await add(service, user)
    async with db() as conn:
        await conn.execute(
            "UPDATE waitlist_entries SET created_at='2026-09-24 19:00+00' WHERE id=$1::uuid", a["id"]
        )
    assert (await service.get_stats(user))["total_today"] == 1


async def test_normal_session_creation_still_works_and_records_branch(db, user, monkeypatch):
    from app.services import table_service

    tid = await table(db, user)

    @asynccontextmanager
    async def transaction():
        async with db() as conn, conn.transaction():
            yield conn

    @asynccontextmanager
    async def lock(*args, **kwargs):
        yield

    monkeypatch.setattr(table_service, "get_transaction", transaction)
    monkeypatch.setattr(table_service, "DistributedLock", lock)
    monkeypatch.setattr(table_service, "emit_and_publish", AsyncMock())
    monkeypatch.setattr(table_service, "cache_delete", AsyncMock())
    result = await table_service.TableSessionService().start_session(user, str(tid), 2)
    async with db() as conn:
        assert (
            await conn.fetchval(
                "SELECT session_token FROM table_sessions WHERE id=$1::uuid", result["session_id"]
            )
            == result["session_token"]
        )


async def test_legacy_end_respects_canonical_unpaid_balance(db, user, service, monkeypatch):
    from app.services import table_service, dinein_session_service

    tid = await table(db, user)
    seated = await service.seat_customer(user, (await add(service, user))["id"], str(tid))
    monkeypatch.setattr(table_service, "get_connection", db)
    monkeypatch.setattr(
        dinein_session_service.DineInSessionService,
        "get_session_bill",
        AsyncMock(return_value={"remaining_amount": 100}),
    )
    with pytest.raises(ValidationError, match="unpaid"):
        await table_service.TableSessionService().end_session(user, seated["session_id"])
    async with db() as conn:
        assert await conn.fetchval(
            "SELECT is_active FROM table_sessions WHERE id=$1::uuid", seated["session_id"]
        )
