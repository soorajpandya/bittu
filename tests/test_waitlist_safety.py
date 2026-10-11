"""Waitlist regression tests with isolated PostgreSQL and no external services."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.core.auth import UserContext
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.services import waitlist_service as module, waitlist_scheduler, waitlist_tables

OWNER = "owner-test"
RID = "11111111-1111-1111-1111-111111111111"
RID2 = "22222222-2222-2222-2222-222222222222"
B1 = "33333333-3333-3333-3333-333333333333"
B2 = "44444444-4444-4444-4444-444444444444"


@pytest.fixture(scope="session")
def waitlist_postgres(tmp_path_factory):
    import pgserver

    server = pgserver.get_server(tmp_path_factory.mktemp("waitlist-postgres"), cleanup_mode="delete")
    yield server.get_uri()
    server.cleanup()


@pytest.fixture
async def db(waitlist_postgres, monkeypatch):
    schema = "wl_" + uuid4().hex
    bootstrap = await asyncpg.connect(waitlist_postgres)
    await bootstrap.execute(f"CREATE SCHEMA {schema}")
    pool = await asyncpg.create_pool(
        waitlist_postgres, min_size=1, max_size=12, server_settings={"search_path": schema}
    )

    @asynccontextmanager
    async def connection():
        async with pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction():
        async with connection() as conn:
            async with conn.transaction():
                yield conn

    for name in ("get_connection", "get_service_connection"):
        monkeypatch.setattr(module, name, connection)
    monkeypatch.setattr(module, "get_transaction", transaction)
    from app.services import push_service

    monkeypatch.setattr(push_service, "get_service_connection", connection)
    monkeypatch.setattr(waitlist_scheduler, "get_service_connection", connection)
    monkeypatch.setattr(module, "emit_and_publish", AsyncMock())
    from app.core import redis

    monkeypatch.setattr(redis, "cache_delete_pattern", AsyncMock())
    monkeypatch.setattr(redis, "cache_delete", AsyncMock())
    monkeypatch.setattr(waitlist_tables, "get_settings", lambda: SimpleNamespace(SESSION_TIMEOUT_MINUTES=120))
    async with connection() as conn:
        await conn.execute("""
            CREATE TABLE restaurants(id uuid PRIMARY KEY,owner_id text,name text,is_active bool DEFAULT true);
            CREATE TABLE sub_branches(id uuid PRIMARY KEY,restaurant_id uuid,owner_id text,is_active bool DEFAULT true,is_main_branch bool DEFAULT false);
            CREATE TABLE restaurant_tables(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),user_id text,restaurant_id uuid,
              table_number text,capacity int,status text DEFAULT 'blank',is_active bool DEFAULT true,is_occupied bool DEFAULT false,
              occupied_since timestamptz,session_token text);
            CREATE TABLE table_sessions(id uuid PRIMARY KEY,table_id uuid,restaurant_id uuid,user_id text,branch_id uuid,
              session_token text,guest_count int,customer_count int,started_at timestamptz,is_active bool,status text,
              expires_at timestamptz,ended_at timestamptz);
            CREATE TABLE dine_in_sessions(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),table_id uuid,status text,
              session_token text,active_order_id uuid,join_pin text,created_at timestamptz DEFAULT now(),
              restaurant_id uuid,user_id text,branch_id uuid,guest_count int,expires_at timestamptz,ended_at timestamptz);
        """)
        sql = Path("migrations/009_waitlist.sql").read_text().split("ALTER TABLE waitlist_entries ENABLE")[0]
        await conn.execute(sql)
        await conn.execute(Path("migrations/087_waitlist_safety.sql").read_text())
        await conn.execute(Path("migrations/064_web_push.sql").read_text())
        await conn.execute(Path("migrations/088_waitlist_delivery_and_sessions.sql").read_text())
        await conn.execute(Path("migrations/088_waitlist_delivery_and_sessions.sql").read_text())
        # Migration is safe to run twice.
        await conn.execute(Path("migrations/087_waitlist_safety.sql").read_text())
        await conn.execute(
            "INSERT INTO restaurants(id,owner_id,name) VALUES($1,$3,'One'),($2,$3,'Two')",
            UUID(RID),
            UUID(RID2),
            OWNER,
        )
    yield connection
    await pool.close()
    await bootstrap.execute(f"DROP SCHEMA {schema} CASCADE")
    await bootstrap.close()


@pytest.fixture
def user():
    return UserContext(user_id=OWNER, restaurant_id=RID)


@pytest.fixture
def service():
    return module.WaitlistService()


async def table(db, user, capacity=4):
    async with db() as conn:
        return await conn.fetchval(
            "INSERT INTO restaurant_tables(user_id,restaurant_id,branch_id,table_number,capacity) VALUES($1,$2::uuid,$3::uuid,$4,$5) RETURNING id",
            user.owner_id if user.is_branch_user else user.user_id,
            user.restaurant_id,
            user.branch_id,
            uuid4().hex[:6],
            capacity,
        )


async def add(service, user, phone=None, party=2):
    return await service.add_entry(user, "Guest", party, phone=phone, notes="private staff note")


async def test_concurrent_joins_have_unique_positions_and_normalized_phone_dedup(db, service, user):
    rows = await asyncio.gather(*(add(service, user) for _ in range(8)))
    assert sorted(row["position"] for row in rows) == list(range(1, 9))
    results = await asyncio.gather(
        add(service, user, "9876543210"), add(service, user, "+91 98765 43210"), return_exceptions=True
    )
    assert sum(isinstance(row, ConflictError) for row in results) == 1
    assert (await service.get_queue(user))["total"] == 9


async def test_concurrent_notifications_reserve_table_once(db, service, user):
    tid = await table(db, user)
    await add(service, user)
    await add(service, user)
    results = await asyncio.gather(
        service.notify_next(user), service.notify_next(user), return_exceptions=True
    )
    assert sum(isinstance(row, ConflictError) for row in results) == 1
    async with db() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM waitlist_entries WHERE assigned_table_id=$1 AND status='notified'", tid
            )
            == 1
        )
    assert sum(row["status"] == "waiting" for row in (await service.get_queue(user))["entries"]) == 1


@pytest.mark.parametrize("capacity", [None, 1])
async def test_no_fitting_table_does_not_notify(db, service, user, capacity):
    if capacity:
        await table(db, user, capacity)
    entry = await add(service, user)
    with pytest.raises(ConflictError):
        await service.notify_next(user)
    assert (await service.get_entry_status(entry["id"]))["status"] == "waiting"
    assert not any(
        e.args[0].payload.get("status") == "notified" for e in module.emit_and_publish.call_args_list
    )


async def test_branch_and_restaurant_isolation(db, service, user):
    other = UserContext(user_id=OWNER, restaurant_id=RID2)
    branch = UserContext(
        user_id="staff", owner_id=OWNER, is_branch_user=True, restaurant_id=RID, branch_id=B1
    )
    first = await add(service, other)
    second = await add(service, branch)
    own = await add(service, user)
    tid = await table(db, user)
    assert (await service.get_queue(user))["total"] == 1
    assert (await service.notify_next(user))["id"] == own["id"]
    for foreign in (first, second):
        with pytest.raises(NotFoundError):
            await service.seat_customer(user, foreign["id"], str(tid))
        with pytest.raises(NotFoundError):
            await service.cancel_entry(user, foreign["id"])
    assert (await service.get_queue(branch))["entries"][0]["id"] == second["id"]


async def test_qr_branch_resolution_and_public_privacy(db, service, user):
    async with db() as conn:
        await conn.execute(
            "INSERT INTO sub_branches(id,restaurant_id,owner_id) VALUES($1,$3,$4),($2,$3,$4)",
            UUID(B1),
            UUID(B2),
            UUID(RID),
            OWNER,
        )
    with pytest.raises(ValidationError):
        await service.add_entry_public(RID, "Customer", 2, "9876543210")
    with pytest.raises(NotFoundError):
        await service.add_entry_public(RID2, "Customer", 2, "9876543210", branch_id=B1)
    entry = await service.add_entry_public(RID, "Customer", 2, "9876543210", notes="private", branch_id=B1)
    status = await service.get_entry_status(entry["id"])
    for result in (entry, status):
        assert not (
            {"phone", "notes", "user_id", "restaurant_id", "branch_id", "seated_session_id", "session_token"}
            & result.keys()
        )
    staff = UserContext(user_id="staff", owner_id=OWNER, is_branch_user=True, restaurant_id=RID, branch_id=B1)
    assert (await service.get_queue(staff))["total"] == 1
    assert (await service.get_queue(user))["total"] == 0


async def test_qr_single_branch_and_disabled_setting(db, service, user):
    async with db() as conn:
        await conn.execute(
            "INSERT INTO sub_branches(id,restaurant_id,owner_id) VALUES($1,$2,$3)", UUID(B1), UUID(RID), OWNER
        )
    entry = await service.add_entry_public(RID, "Customer", 2, "9876543210")
    async with db() as conn:
        assert (
            str(await conn.fetchval("SELECT branch_id FROM waitlist_entries WHERE id=$1::uuid", entry["id"]))
            == B1
        )
    await service.update_settings(user, {"qr_entry_enabled": False})
    with pytest.raises(ConflictError):
        await service.add_entry_public(RID, "Other", 2, "9876543211")


async def test_seating_atomic_and_replay_does_not_reoccupy(db, service, user):
    tid = await table(db, user)
    entry = await add(service, user)
    await service.notify_next(user)
    seated = await service.seat_customer(user, entry["id"])
    replay = await service.seat_customer(user, entry["id"], str(tid))
    assert replay["session_id"] == seated["session_id"]
    assert replay["session_token"] == seated["session_token"]
    async with db() as conn:
        assert await conn.fetchval("SELECT count(*) FROM table_sessions") == 1
        await conn.execute("UPDATE table_sessions SET is_active=false,status='ended'")
        await conn.execute("UPDATE restaurant_tables SET status='blank',is_occupied=false,session_token=NULL")
    replay = await service.seat_customer(user, entry["id"])
    assert "session_token" not in replay
    async with db() as conn:
        assert not await conn.fetchval("SELECT is_occupied FROM restaurant_tables WHERE id=$1", tid)
    assert not any("session_token" in call.args[0].payload for call in module.emit_and_publish.call_args_list)


async def test_seating_rollback_rolls_back_session_and_occupancy(db, service, user, monkeypatch):
    tid = await table(db, user)
    entry = await add(service, user)
    monkeypatch.setattr(service, "_log_action", AsyncMock(side_effect=RuntimeError("audit failed")))
    with pytest.raises(RuntimeError, match="audit failed"):
        await service.seat_customer(user, entry["id"], str(tid))
    async with db() as conn:
        assert await conn.fetchval("SELECT count(*) FROM table_sessions") == 0
        assert not await conn.fetchval("SELECT is_occupied FROM restaurant_tables WHERE id=$1", tid)
    assert (await service.get_entry_status(entry["id"]))["status"] == "waiting"


async def test_seat_requires_valid_table_and_rejects_another_reservation(db, service, user):
    tid = await table(db, user)
    first, second = await add(service, user), await add(service, user)
    await service.notify_next(user)
    with pytest.raises(ValidationError):
        await service.seat_customer(user, second["id"])
    with pytest.raises(ConflictError, match="reserved"):
        await service.seat_customer(user, second["id"], str(tid))
    other = UserContext(user_id=OWNER, restaurant_id=RID2)
    other_tid = await table(db, other)
    with pytest.raises(NotFoundError):
        await service.seat_customer(user, second["id"], str(other_tid))
    too_small = await table(db, user, 1)
    with pytest.raises(ValidationError):
        await service.seat_customer(user, second["id"], str(too_small))
    await service.cancel_entry(user, second["id"])
    with pytest.raises(ConflictError):
        await service.seat_customer(user, second["id"], str(tid))


async def test_two_parties_cannot_seat_at_same_table(db, service, user):
    tid = await table(db, user)
    entries = [await add(service, user), await add(service, user)]
    results = await asyncio.gather(
        *(service.seat_customer(user, row["id"], str(tid)) for row in entries), return_exceptions=True
    )
    assert sum(isinstance(row, ConflictError) for row in results) == 1
    async with db() as conn:
        assert await conn.fetchval("SELECT count(*) FROM waitlist_entries WHERE status='seated'") == 1
        assert await conn.fetchval("SELECT count(*) FROM table_sessions") == 1


async def test_existing_session_never_attached_to_waiting_party(db, service, user):
    tid = await table(db, user)
    entry = await add(service, user)
    async with db() as conn:
        await conn.execute("INSERT INTO dine_in_sessions(table_id,status) VALUES($1,'active')", tid)
    with pytest.raises(ConflictError, match="active session"):
        await service.seat_customer(user, entry["id"], str(tid))
    assert (await service.get_entry_status(entry["id"]))["status"] == "waiting"


async def test_reorder_validates_full_scope_before_mutating(db, service, user):
    entries = [await add(service, user) for _ in range(3)]
    ids = [row["id"] for row in entries]
    with pytest.raises(ValidationError):
        await service.reorder(user, [ids[0], ids[0], ids[2]])
    with pytest.raises(ConflictError):
        await service.reorder(user, ids[:2])
    with pytest.raises(ConflictError):
        await service.reorder(user, [ids[0], ids[1], str(uuid4())])
    assert [row["id"] for row in (await service.get_queue(user))["entries"]] == ids
    result = await service.reorder(user, list(reversed(ids)))
    assert [row["position"] for row in result] == [1, 2, 3]
    assert [row["id"] for row in (await service.get_queue(user))["entries"]] == list(reversed(ids))


async def test_expiry_releases_reservation_and_broadcasts(db, service, user):
    tid = await table(db, user)
    first, second = await add(service, user), await add(service, user)
    await service.notify_next(user)
    async with db() as conn:
        await conn.execute(
            "UPDATE waitlist_entries SET expires_at=now()-interval '1 minute' WHERE id=$1::uuid", first["id"]
        )
    with pytest.raises(ConflictError, match="expired"):
        await service.seat_customer(user, first["id"])
    assert (await service.expire_overdue(user))[0]["status"] == "skipped"
    notified = await service.notify_next(user)
    assert notified["id"] == second["id"] and notified["assigned_table_id"] == str(tid)
    assert (await service.get_entry_status(second["id"]))["position"] == 1
    assert any(
        call.args[0].payload.get("status") == "skipped" for call in module.emit_and_publish.call_args_list
    )


async def test_worker_uses_default_auto_notify_and_honors_disabled_setting(db, service, user):
    await table(db, user)
    entry = await add(service, user)
    await waitlist_scheduler.run_waitlist_tick()
    assert (await service.get_entry_status(entry["id"]))["status"] == "notified"
    await service.update_settings(user, {"auto_notify": False})
    async with db() as conn:
        await conn.execute("UPDATE waitlist_entries SET expires_at=now()-interval '1 minute'")
    second = await add(service, user)
    await waitlist_scheduler.run_waitlist_tick()
    assert (await service.get_entry_status(entry["id"]))["status"] == "skipped"
    assert (await service.get_entry_status(second["id"]))["status"] == "waiting"


async def test_public_display_respects_toggle_and_counts_entire_queue(db, service, user):
    for _ in range(23):
        await add(service, user)
    with pytest.raises(NotFoundError):
        await service.get_display_data(RID)
    await service.update_settings(user, {"display_screen_enabled": True})
    assert (await service.get_display_data(RID))["total_waiting"] == 23


async def test_every_queue_mutation_broadcasts_and_invalidates_cache(db, service, user):
    from app.core import redis

    first, second = await add(service, user), await add(service, user)
    await service.reorder(user, [second["id"], first["id"]])
    await service.skip_customer(user, first["id"])
    await service.cancel_entry(user, second["id"])
    assert module.emit_and_publish.await_count == 5
    assert redis.cache_delete_pattern.await_count == 5


async def test_read_only_permission_cannot_mutate_waitlist(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app.api.v1.waitlist import router
    from app.core import auth

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[auth.get_current_user] = lambda: UserContext(
        user_id=OWNER, restaurant_id=RID, role="staff"
    )

    async def check(user, permission):
        return SimpleNamespace(
            allowed=permission == "waitlist.read", permission_key=permission, meta={}, role_id=None
        )

    monkeypatch.setattr(auth.rbac_service, "check_permission", check)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for method, path, body in [
            ("POST", "/waitlist", {"customer_name": "Guest", "party_size": 2}),
            ("POST", "/waitlist/expire-check", None),
            ("PATCH", f"/waitlist/{uuid4()}/cancel", None),
        ]:
            response = await client.request(method, path, json=body)
            assert response.status_code == 403


async def test_table_event_wakes_worker_without_database_access(monkeypatch):
    wake = asyncio.Event()
    monkeypatch.setattr(waitlist_scheduler, "_wake", wake)
    await waitlist_scheduler._on_table_change(SimpleNamespace(payload={"status": "blank"}))
    assert wake.is_set()


async def test_seating_and_cancel_race_has_one_terminal_result(db, service, user):
    tid = await table(db, user)
    entry = await add(service, user)
    results = await asyncio.gather(
        service.seat_customer(user, entry["id"], str(tid)),
        service.cancel_entry(user, entry["id"]),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    status = (await service.get_entry_status(entry["id"]))["status"]
    async with db() as conn:
        assert await conn.fetchval("SELECT count(*) FROM table_sessions") == (1 if status == "seated" else 0)


async def test_concurrent_workers_do_not_notify_same_table_twice(db, service, user):
    await table(db, user)
    await add(service, user)
    await add(service, user)
    await asyncio.gather(waitlist_scheduler.run_waitlist_tick(), waitlist_scheduler.run_waitlist_tick())
    async with db() as conn:
        assert await conn.fetchval("SELECT count(*) FROM waitlist_entries WHERE status='notified'") == 1


async def test_explicit_main_branch_resolves_legacy_qr_link(db, service):
    async with db() as conn:
        await conn.execute(
            "INSERT INTO sub_branches(id,restaurant_id,owner_id,is_main_branch) VALUES($1,$3,$4,true),($2,$3,$4,false)",
            UUID(B1),
            UUID(B2),
            UUID(RID),
            OWNER,
        )
    assert (await service.public_context(RID)).branch_id == B1
    assert (await service.public_context(RID, B2)).branch_id == B2


async def test_branch_reorder_does_not_touch_another_branch(db, service, user):
    branch = UserContext(
        user_id="staff", owner_id=OWNER, is_branch_user=True, restaurant_id=RID, branch_id=B1
    )
    main = await add(service, user)
    first, second = await add(service, branch), await add(service, branch)
    await service.reorder(branch, [second["id"], first["id"]])
    assert (await service.get_entry_status(first["id"]))["position"] == 2
    assert (await service.get_entry_status(main["id"]))["position"] == 1
    with pytest.raises(ConflictError):
        await service.reorder(branch, [main["id"], first["id"]])


async def test_reservation_blocks_normal_session_and_both_qr_flows(db, service, user, monkeypatch):
    from app.services import table_service, dinein_session_service

    tid = await table(db, user)
    await add(service, user)
    await service.notify_next(user)
    async with db() as conn:
        await conn.execute(
            "ALTER TABLE restaurants ADD COLUMN logo_url text, ADD COLUMN phone text, ADD COLUMN address text, ADD COLUMN city text"
        )

    @asynccontextmanager
    async def tx():
        async with db() as conn:
            async with conn.transaction():
                yield conn

    @asynccontextmanager
    async def lock(*args, **kwargs):
        yield

    for mod in (table_service, dinein_session_service):
        monkeypatch.setattr(mod, "get_connection", db)
        monkeypatch.setattr(mod, "get_transaction", tx)
    monkeypatch.setattr(table_service, "get_serializable_transaction", tx)
    monkeypatch.setattr(table_service, "DistributedLock", lock)
    with pytest.raises(ConflictError, match="reserved"):
        await table_service.TableSessionService().start_session(user, str(tid), 2)
    with pytest.raises(ConflictError, match="reserved"):
        await table_service.TableSessionService().qr_scan(RID, str(tid), "device")
    with pytest.raises(ConflictError, match="reserved"):
        await dinein_session_service.DineInSessionService().scan_qr(RID, str(tid), "device")
    async with db() as conn:
        assert await conn.fetchval("SELECT count(*) FROM table_sessions") == 0
        assert await conn.fetchval("SELECT count(*) FROM dine_in_sessions") == 0


async def test_migration_repairs_legacy_assignments_without_losing_parties(db, service, user):
    tid = await table(db, user)
    first, second = await add(service, user), await add(service, user)
    async with db() as conn:
        await conn.execute("DROP INDEX waitlist_one_notified_party_per_table")
        await conn.execute(
            "INSERT INTO sub_branches(id,restaurant_id,owner_id,is_main_branch) VALUES($1,$2,$3,true)",
            UUID(B1),
            UUID(RID),
            OWNER,
        )
        await conn.execute(
            "UPDATE waitlist_entries SET status='notified',assigned_table_id=$1,notified_at=now(),source='qr'",
            tid,
        )
        await conn.execute(Path("migrations/087_waitlist_safety.sql").read_text())
        rows = await conn.fetch("SELECT status,branch_id FROM waitlist_entries ORDER BY created_at")
        assert [row["status"] for row in rows] == ["notified", "waiting"]
        assert all(str(row["branch_id"]) == B1 for row in rows)
        assert str(await conn.fetchval("SELECT branch_id FROM restaurant_tables WHERE id=$1", tid)) == B1
        await conn.execute(Path("migrations/087_waitlist_safety.sql").read_text())
        assert await conn.fetchval("SELECT count(*) FROM waitlist_entries") == 2


async def test_event_failure_cannot_roll_back_committed_seating(db, service, user, monkeypatch):
    tid = await table(db, user)
    entry = await add(service, user)
    monkeypatch.setattr(module, "emit_and_publish", AsyncMock(side_effect=RuntimeError("pubsub down")))
    seated = await service.seat_customer(user, entry["id"], str(tid))
    assert seated["status"] == "seated"
    replay = await service.seat_customer(user, entry["id"], str(tid))
    assert replay["session_id"] == seated["session_id"]


async def test_qr_page_and_submission_preserve_branch(db, service, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app.api.v1 import waitlist

    async with db() as conn:
        await conn.execute(
            "INSERT INTO sub_branches(id,restaurant_id,owner_id) VALUES($1,$2,$3)", UUID(B1), UUID(RID), OWNER
        )
    monkeypatch.setattr(waitlist, "get_service_connection", db)
    app = FastAPI()
    app.include_router(waitlist.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get(f"/waitlist/qr/{RID}?branch_id={B1}")
        assert page.status_code == 200
        assert f'var BID = "{B1}"' in page.text
        assert "branch_id:BID || null" in page.text
        response = await client.post(
            f"/waitlist/public/{RID}",
            json={
                "customer_name": "Guest",
                "party_size": 2,
                "phone": "9876543210",
                "branch_id": B1,
            },
        )
        assert response.status_code == 200
        assert "phone" not in response.json()
        result = await client.get("/waitlist/status/" + response.json()["id"])
        assert result.status_code == 200
        assert "phone" not in result.json() and "notes" not in result.json()
