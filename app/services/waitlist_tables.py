"""Table-row locking shared by waitlist seating and dine-in entry points."""

import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError


async def assert_table_not_reserved(conn, table_id, entry_id=None):
    """Caller holds the table row lock until its session transaction commits."""
    reserved = await conn.fetchval(
        "SELECT id FROM waitlist_entries WHERE assigned_table_id=$1::uuid "
        "AND status='notified' AND (expires_at IS NULL OR expires_at>now()) "
        "AND ($2::uuid IS NULL OR id<>$2::uuid) LIMIT 1",
        str(table_id),
        str(entry_id) if entry_id else None,
    )
    if reserved:
        raise ConflictError("Table is reserved for a notified waitlist party")


async def create_seating_session(conn, user, table_id, guest_count, entry_id):
    """Create the session in the SAME transaction as the waitlist transition."""
    owner = user.owner_id if user.is_branch_user else user.user_id
    table = await conn.fetchrow(
        "SELECT * FROM restaurant_tables WHERE id=$1::uuid AND user_id=$2 "
        "AND restaurant_id=$3::uuid AND branch_id IS NOT DISTINCT FROM $4::uuid FOR UPDATE",
        table_id,
        str(owner),
        str(user.restaurant_id),
        str(user.branch_id) if user.branch_id else None,
    )
    if not table:
        raise NotFoundError("Table", table_id)
    if not table["is_active"] or table["is_occupied"] or table["status"] not in {"blank", "available"}:
        raise ConflictError("Table is not available")
    if guest_count < 1 or guest_count > (table["capacity"] or 0):
        raise ValidationError("Party does not fit this table")
    await assert_table_not_reserved(conn, table_id, entry_id)
    active = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM table_sessions WHERE table_id=$1::uuid AND is_active "
        "AND (expires_at IS NULL OR expires_at>now())) "
        "OR EXISTS(SELECT 1 FROM dine_in_sessions WHERE table_id=$1::uuid AND status='active')",
        table_id,
    )
    if active:
        raise ConflictError("Table already has an active session")
    await conn.execute(
        "UPDATE table_sessions SET is_active=false,status='expired',ended_at=COALESCE(ended_at,now()) "
        "WHERE table_id=$1::uuid AND is_active AND expires_at<=now()",
        table_id,
    )
    session_id, token = str(uuid4()), secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(minutes=get_settings().SESSION_TIMEOUT_MINUTES)
    await conn.execute(
        "INSERT INTO table_sessions(id,table_id,restaurant_id,user_id,branch_id,session_token,guest_count,customer_count,started_at,is_active,status,expires_at) "
        "VALUES($1::uuid,$2::uuid,$3::uuid,$4,$5::uuid,$6,$7,$7,now(),true,'active',$8)",
        session_id,
        table_id,
        str(user.restaurant_id),
        str(owner),
        str(user.branch_id) if user.branch_id else None,
        token,
        guest_count,
        expires,
    )
    await conn.execute(
        "INSERT INTO dine_in_sessions(id,legacy_session_id,table_id,restaurant_id,user_id,branch_id,session_token,guest_count,status,expires_at,join_pin) "
        "VALUES($1::uuid,$1::uuid,$2::uuid,$3::uuid,$4,$5::uuid,$6,$7,'active',$8,$9)",
        session_id,
        table_id,
        str(user.restaurant_id),
        str(owner),
        str(user.branch_id) if user.branch_id else None,
        token,
        guest_count,
        expires,
        f"{secrets.randbelow(10000):04d}",
    )
    await conn.execute(
        "UPDATE restaurant_tables SET status='running',is_occupied=true,occupied_since=now(),session_token=$2 WHERE id=$1::uuid",
        table_id,
        token,
    )
    return {"session_id": session_id, "session_token": token, "table_number": table["table_number"]}


async def assert_table_scope(conn, user, identifier):
    """Accept a table or either session ID, always through the table's scope."""
    owner = user.owner_id if user.is_branch_user else user.user_id
    if not user.restaurant_id or (user.is_branch_user and not user.branch_id):
        raise NotFoundError("Table", str(identifier))
    found = await conn.fetchval(
        "SELECT t.id FROM restaurant_tables t WHERE t.user_id=$2 AND t.restaurant_id=$3::uuid "
        "AND t.branch_id IS NOT DISTINCT FROM $4::uuid AND (t.id=$1::uuid "
        "OR EXISTS(SELECT 1 FROM table_sessions s WHERE s.id=$1::uuid AND s.table_id=t.id) "
        "OR EXISTS(SELECT 1 FROM dine_in_sessions s WHERE s.id=$1::uuid AND s.table_id=t.id))",
        str(identifier),
        str(owner),
        str(user.restaurant_id),
        str(user.branch_id) if user.branch_id else None,
    )
    if not found:
        raise NotFoundError("Table", str(identifier))
    return found
