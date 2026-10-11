"""Waitlist queues, table reservations, and atomic seating per restaurant location."""

import hashlib
import secrets
import os
from zoneinfo import ZoneInfo
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import structlog

from app.core.database import get_connection, get_transaction, get_service_connection, use_tenant
from app.core.auth import UserContext
from app.core.exceptions import NotFoundError, ConflictError, ValidationError
from app.core.events import DomainEvent, emit_and_publish, TABLE_SESSION_STARTED

logger = structlog.get_logger(__name__)


def _owner(user):
    return user.owner_id if user.is_branch_user else user.user_id


def _scope(user, alias="", offset=0):
    if not user.restaurant_id or not _owner(user) or (user.is_branch_user and not user.branch_id):
        raise ValidationError("Restaurant and branch context are required")
    prefix = f"{alias}." if alias else ""
    return (
        f"{prefix}user_id = ${offset+1} AND {prefix}restaurant_id = ${offset+2}::uuid "
        f"AND {prefix}branch_id IS NOT DISTINCT FROM ${offset+3}::uuid",
        [str(_owner(user)), str(user.restaurant_id), str(user.branch_id) if user.branch_id else None],
    )


async def _lock_queue(conn, user):
    _scope(user)
    # Transaction-scoped lock works through PgBouncer and serializes all queue
    # writers, including joins, cancellation, expiry and notification workers.
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"waitlist:{user.restaurant_id}"
    )


def _phone(value, validate=True):
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    # Indian local and +91 formats must identify the same customer.
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if validate and not 6 <= len(digits) <= 15:
        raise ValidationError("Enter a valid phone number")
    return digits


async def _broadcast_waitlist(event_type, restaurant_id, entry):
    # Invalidate every branch cache for this restaurant after commit, including
    # public joins and worker changes which have no authenticated HTTP caller.
    try:
        from app.core.redis import cache_delete_pattern

        await cache_delete_pattern(f"route:waitlist:{restaurant_id}:*")
    except Exception:
        logger.exception("waitlist_cache_invalidation_failed")
    try:
        await emit_and_publish(
            DomainEvent(
                event_type=event_type,
                payload={
                    key: value
                    for key, value in entry.items()
                    if key not in {"session_token", "push_token_hash", "phone", "notes"}
                },
                restaurant_id=str(restaurant_id),
                branch_id=str(entry["branch_id"]) if entry.get("branch_id") else None,
            )
        )
    except Exception:
        logger.exception("waitlist_event_publish_failed", event_type=event_type)


class WaitlistService:
    async def public_context(self, restaurant_id, branch_id=None):
        async with get_service_connection() as conn:
            rest = await conn.fetchrow(
                "SELECT owner_id FROM restaurants WHERE id=$1::uuid AND is_active", str(restaurant_id)
            )
            if not rest:
                raise NotFoundError("Restaurant", str(restaurant_id))
            branches = await conn.fetch(
                "SELECT id,is_main_branch FROM sub_branches WHERE restaurant_id=$1::uuid AND is_active ORDER BY id",
                str(restaurant_id),
            )
        if branch_id:
            if str(branch_id) not in {str(row["id"]) for row in branches}:
                raise NotFoundError("Branch", str(branch_id))
        elif len(branches) == 1:
            branch_id = branches[0]["id"]
        elif branches:
            main = [row for row in branches if row["is_main_branch"]]
            if len(main) != 1:
                raise ValidationError("Use the waitlist QR code for your branch")
            branch_id = main[0]["id"]
        return UserContext(
            user_id=str(rest["owner_id"]),
            restaurant_id=str(restaurant_id),
            branch_id=str(branch_id) if branch_id else None,
        )

    async def add_entry(self, user, customer_name, party_size, phone=None, source="staff", notes=None):
        if not customer_name.strip() or not 1 <= party_size <= 50 or source not in {"staff", "qr"}:
            raise ValidationError("Invalid waitlist entry")
        phone = _phone(phone)
        clause, params = _scope(user)
        async with get_transaction() as conn:
            await _lock_queue(conn, user)
            settings = await self._get_settings(conn, user) or self._default_settings()
            if source == "qr" and not settings["qr_entry_enabled"]:
                raise ConflictError("QR self-service waitlist entry is disabled for this restaurant")
            if phone:
                phones = await conn.fetch(
                    f"SELECT phone FROM waitlist_entries WHERE {clause} AND status IN ('waiting','notified') AND phone IS NOT NULL",
                    *params,
                )
                if any(_phone(row["phone"], validate=False) == phone for row in phones):
                    raise ConflictError("Customer is already on this waitlist")
            position = 1 + await conn.fetchval(
                f"SELECT COALESCE(MAX(position),0) FROM waitlist_entries WHERE {clause} AND status IN ('waiting','notified')",
                *params,
            )
            estimate = None
            row = await conn.fetchrow(
                "INSERT INTO waitlist_entries (user_id,restaurant_id,branch_id,customer_name,phone,party_size,source,status,position,estimated_wait_minutes,notes) "
                "VALUES($1,$2::uuid,$3::uuid,$4,$5,$6,$7,'waiting',$8,$9,$10) RETURNING *",
                *params,
                customer_name.strip(),
                phone,
                party_size,
                source,
                position,
                estimate,
                notes,
            )
            await self._reposition(conn, user)
            row = await conn.fetchrow("SELECT * FROM waitlist_entries WHERE id=$1", row["id"])
            await self._log_action(
                conn, user.restaurant_id, row["id"], "added", {"source": source}, user.user_id
            )
        result = self._format_entry(row)
        await _broadcast_waitlist("waitlist.created", user.restaurant_id, result)
        return result

    async def add_entry_public(
        self, restaurant_id, customer_name, party_size, phone, notes=None, branch_id=None
    ):
        user = await self.public_context(restaurant_id, branch_id)
        async with use_tenant(user.user_id):
            result = await self.add_entry(user, customer_name, party_size, phone, "qr", notes)
        token = secrets.token_urlsafe(32)
        async with get_service_connection() as conn:
            await conn.execute(
                "UPDATE waitlist_entries SET push_token_hash=$2 WHERE id=$1::uuid",
                result["id"],
                hashlib.sha256(token.encode()).hexdigest(),
            )
        return {**self._public_entry(result), "push_token": token}

    async def get_queue(self, user, status=None, limit=50, offset=0):
        clause, params = _scope(user, "w")
        if status:
            clause += " AND w.status=$4"
            params.append(status)
        else:
            clause += " AND w.status IN ('waiting','notified')"
        async with get_connection() as conn:
            total = await conn.fetchval(f"SELECT COUNT(*) FROM waitlist_entries w WHERE {clause}", *params)
            params.extend([limit, offset])
            rows = await conn.fetch(
                "SELECT w.*,rt.table_number,rt.capacity AS table_capacity,j.state AS delivery_status FROM waitlist_entries w LEFT JOIN waitlist_push_jobs j ON j.entry_id=w.id "
                f"LEFT JOIN restaurant_tables rt ON rt.id=w.assigned_table_id WHERE {clause} "
                f"ORDER BY w.position,w.created_at,w.id LIMIT ${len(params)-1} OFFSET ${len(params)}",
                *params,
            )
        return {"total": total, "entries": [self._format_entry(row) for row in rows]}

    @staticmethod
    def _public_entry(row):
        fields = (
            "id",
            "customer_name",
            "party_size",
            "status",
            "position",
            "estimated_wait_minutes",
            "notified_at",
            "expires_at",
            "seated_at",
            "table_number",
            "assigned_table_number",
            "delivery_status",
        )
        return {key: value for key, value in WaitlistService._format_entry(row).items() if key in fields}

    async def get_entry_status(self, entry_id):
        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                "SELECT w.*,rt.table_number,j.state AS delivery_status FROM waitlist_entries w LEFT JOIN waitlist_push_jobs j ON j.entry_id=w.id LEFT JOIN restaurant_tables rt ON rt.id=w.assigned_table_id WHERE w.id=$1::uuid",
                str(entry_id),
            )
        return self._public_entry(row) if row else None

    async def notify_next(self, user, table_id=None):
        await self.expire_overdue(user)
        clause, params = _scope(user)
        async with get_transaction() as conn:
            await _lock_queue(conn, user)
            settings = await self._get_settings(conn, user) or self._default_settings()
            waiting = await conn.fetch(
                f"SELECT * FROM waitlist_entries WHERE {clause} AND status='waiting' ORDER BY position,created_at,id FOR UPDATE",
                *params,
            )
            if not waiting:
                return None
            table_clause, table_params = _scope(user, "t")
            if table_id:
                table_clause += " AND t.id=$4::uuid"
                table_params.append(str(table_id))
            tables = await conn.fetch(
                f"SELECT t.* FROM restaurant_tables t WHERE {table_clause} AND t.is_active "
                "AND t.status IN ('blank','available') AND NOT t.is_occupied "
                "ORDER BY t.capacity,t.id FOR UPDATE OF t",
                *table_params,
            )
            matched = table = None
            for candidate in tables:
                if await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM waitlist_entries WHERE assigned_table_id=$1 AND status='notified') "
                    "OR EXISTS(SELECT 1 FROM table_sessions WHERE table_id=$1 AND is_active AND (expires_at IS NULL OR expires_at>now())) "
                    "OR EXISTS(SELECT 1 FROM dine_in_sessions WHERE table_id=$1 AND status='active')",
                    candidate["id"],
                ):
                    continue
                fits = [row for row in waiting if row["party_size"] <= (candidate["capacity"] or 0)]
                if fits:
                    matched = (
                        max(fits, key=lambda row: row["party_size"])
                        if settings["best_fit_enabled"]
                        else fits[0]
                    )
                    table = candidate
                    break
            if not matched:
                raise ConflictError("No available table fits the waiting parties")
            row = await conn.fetchrow(
                "UPDATE waitlist_entries SET status='notified',notified_at=now(),expires_at=now()+$1::interval,assigned_table_id=$2 "
                "WHERE id=$3 AND status='waiting' RETURNING *",
                timedelta(minutes=settings["notify_expiry_minutes"]),
                table["id"],
                matched["id"],
            )
            await self._reposition(conn, user)
            await self._log_action(
                conn, user.restaurant_id, row["id"], "notified", {"table_id": str(table["id"])}, user.user_id
            )
        result = self._format_entry(row)
        result["assigned_table_number"] = table["table_number"]
        await _broadcast_waitlist("waitlist.updated", user.restaurant_id, result)
        return result

    async def seat_customer(self, user, entry_id, table_id=None, guest_count=None):
        from app.services.waitlist_tables import create_seating_session

        clause, params = _scope(user)
        async with get_transaction() as conn:
            await _lock_queue(conn, user)
            entry = await conn.fetchrow(
                f"SELECT * FROM waitlist_entries WHERE {clause} AND id=$4::uuid FOR UPDATE",
                *params,
                str(entry_id),
            )
            if not entry:
                raise NotFoundError("Waitlist entry", str(entry_id))
            if entry["status"] == "seated":
                if table_id and str(entry["assigned_table_id"]) != str(table_id):
                    raise ConflictError("Customer is already seated at another table")
                result = self._format_entry(entry)
                # Replay only the original session; never reoccupy a table or
                # return credentials belonging to a later party.
                session = (
                    await conn.fetchrow(
                        "SELECT * FROM table_sessions WHERE id=$1", entry["seated_session_id"]
                    )
                    if entry["seated_session_id"]
                    else None
                )
                if session:
                    result["session_id"] = str(session["id"])
                    if session["is_active"] and (
                        not session["expires_at"] or session["expires_at"] > datetime.now(timezone.utc)
                    ):
                        result["session_token"] = session["session_token"]
                return result
            if entry["status"] not in {"waiting", "notified"}:
                raise ConflictError(f"Cannot seat entry in status '{entry['status']}'")
            if (
                entry["status"] == "notified"
                and entry["expires_at"]
                and entry["expires_at"] <= datetime.now(timezone.utc)
            ):
                raise ConflictError("Notification expired; add the party to the queue again")
            target = table_id or entry["assigned_table_id"]
            if not target:
                raise ValidationError("A table is required to seat a party")
            guests = guest_count if guest_count is not None else entry["party_size"]
            if guests < entry["party_size"] or guests > 50:
                raise ValidationError("Guest count must include the entire waiting party (maximum 50)")
            session = await create_seating_session(conn, user, str(target), guests, str(entry_id))
            row = await conn.fetchrow(
                "UPDATE waitlist_entries SET status='seated',seated_at=now(),expires_at=NULL,assigned_table_id=$2::uuid,seated_session_id=$3::uuid "
                "WHERE id=$1::uuid RETURNING *",
                str(entry_id),
                str(target),
                session["session_id"],
            )
            await self._log_action(
                conn, user.restaurant_id, entry_id, "seated", {"table_id": str(target)}, user.user_id
            )
            await self._reposition(conn, user)
        result = {**self._format_entry(row), **session}
        await _broadcast_waitlist("waitlist.updated", user.restaurant_id, result)
        try:
            from app.core.redis import cache_delete

            await cache_delete(f"tables_list:{_owner(user)}")
            await emit_and_publish(
                DomainEvent(
                    event_type=TABLE_SESSION_STARTED,
                    payload={
                        "session_id": session["session_id"],
                        "table_id": str(target),
                        "guest_count": guests,
                    },
                    user_id=user.user_id,
                    restaurant_id=str(user.restaurant_id),
                    branch_id=user.branch_id,
                )
            )
        except Exception:
            logger.exception("waitlist_seating_notification_failed")
        return result

    async def _finish_entry(self, user, entry_id, status, reason=None):
        clause, params = _scope(user)
        async with get_transaction() as conn:
            await _lock_queue(conn, user)
            entry = await conn.fetchrow(
                f"SELECT * FROM waitlist_entries WHERE {clause} AND id=$4::uuid FOR UPDATE",
                *params,
                str(entry_id),
            )
            if not entry:
                raise NotFoundError("Waitlist entry", str(entry_id))
            if entry["status"] == status:
                return self._format_entry(entry)
            if entry["status"] not in {"waiting", "notified"}:
                raise ConflictError(f"Cannot {status} entry in status '{entry['status']}'")
            row = await conn.fetchrow(
                "UPDATE waitlist_entries SET status=$2,assigned_table_id=NULL,expires_at=NULL WHERE id=$1::uuid RETURNING *",
                str(entry_id),
                status,
            )
            await self._log_action(
                conn, user.restaurant_id, entry_id, status, {"reason": reason}, user.user_id
            )
            await self._reposition(conn, user)
        result = self._format_entry(row)
        await _broadcast_waitlist("waitlist.updated", user.restaurant_id, result)
        return result

    async def skip_customer(self, user, entry_id, reason="no_show"):
        return await self._finish_entry(user, entry_id, "skipped", reason)

    async def cancel_entry(self, user, entry_id):
        return await self._finish_entry(user, entry_id, "cancelled")

    async def reorder(self, user, ordered_ids):
        ids = [str(value) for value in ordered_ids]
        if len(ids) != len(set(ids)):
            raise ValidationError("Queue order contains duplicate entries")
        clause, params = _scope(user)
        async with get_transaction() as conn:
            await _lock_queue(conn, user)
            active = await conn.fetch(
                f"SELECT id FROM waitlist_entries WHERE {clause} AND status IN ('waiting','notified') FOR UPDATE",
                *params,
            )
            if set(ids) != {str(row["id"]) for row in active}:
                raise ConflictError(
                    "Supply every active entry from this location exactly once; refresh the queue"
                )
            rows = []
            for position, entry_id in enumerate(ids, 1):
                row = await conn.fetchrow(
                    "UPDATE waitlist_entries SET position=$1 WHERE id=$2::uuid RETURNING *",
                    position,
                    entry_id,
                )
                rows.append(self._format_entry(row))
            await self._reposition(conn, user)
            rows = [
                self._format_entry(r)
                for r in await conn.fetch(
                    f"SELECT * FROM waitlist_entries WHERE {clause} AND status IN ('waiting','notified') ORDER BY position",
                    *params,
                )
            ]
            await self._log_action(
                conn, user.restaurant_id, None, "reordered", {"new_order": ids}, user.user_id
            )
        await _broadcast_waitlist(
            "waitlist.updated",
            user.restaurant_id,
            {"action": "reordered", "branch_id": user.branch_id, "ordered_ids": ids},
        )
        return rows

    async def expire_overdue(self, user):
        clause, params = _scope(user)
        async with get_transaction() as conn:
            await _lock_queue(conn, user)
            rows = await conn.fetch(
                f"UPDATE waitlist_entries SET status='skipped',assigned_table_id=NULL,expires_at=NULL WHERE {clause} "
                "AND status='notified' AND expires_at<=now() RETURNING *",
                *params,
            )
            for row in rows:
                await self._log_action(
                    conn, user.restaurant_id, row["id"], "skipped", {"reason": "expired"}, "system"
                )
            if rows:
                await self._reposition(conn, user)
        results = [self._format_entry(row) for row in rows]
        for result in results:
            await _broadcast_waitlist("waitlist.updated", user.restaurant_id, result)
        return results

    async def on_table_freed(self, user, table_id):
        async with get_connection() as conn:
            settings = await self._get_settings(conn, user) or self._default_settings()
        if not settings["auto_notify"]:
            return None
        try:
            return await self.notify_next(user, table_id)
        except ConflictError:
            return None

    # ─── Settings CRUD ────────────────────────────────────────

    async def get_settings(self, user: UserContext) -> dict:
        async with get_connection() as conn:
            return await self._get_settings(conn, user) or self._default_settings()

    async def update_settings(self, user: UserContext, data: dict) -> dict:
        if data.get("sms_enabled") or data.get("whatsapp_enabled"):
            raise ValidationError(
                "SMS and WhatsApp delivery are unavailable; use browser push or staff notification"
            )
        tenant = {"user_id": _owner(user)}

        allowed = [
            "notify_expiry_minutes",
            "avg_turnover_minutes",
            "sms_enabled",
            "whatsapp_enabled",
            "display_screen_enabled",
            "qr_entry_enabled",
            "auto_notify",
            "best_fit_enabled",
            "display_message",
        ]
        fields = {k: v for k, v in data.items() if k in allowed and v is not None}
        if not fields:
            return await self.get_settings(user)

        async with get_transaction() as conn:
            await _lock_queue(conn, user)
            existing = await self._get_settings(conn, user)

            if existing:
                set_parts, vals = [], []
                for k, v in fields.items():
                    vals.append(v)
                    set_parts.append(f"{k} = ${len(vals)}")
                vals.append(existing["id"])
                row = await conn.fetchrow(
                    f"""UPDATE waitlist_settings SET {', '.join(set_parts)}
                        WHERE id = ${len(vals)} RETURNING *""",
                    *vals,
                )
            else:
                # Insert new settings
                cols = ["restaurant_id", "user_id"] + list(fields.keys())
                vals = [user.restaurant_id, tenant["user_id"]] + list(fields.values())
                placeholders = ", ".join(f"${i+1}" for i in range(len(vals)))
                row = await conn.fetchrow(
                    f"""INSERT INTO waitlist_settings ({', '.join(cols)})
                        VALUES ({placeholders}) RETURNING *""",
                    *vals,
                )

            await self._reposition(conn, user)

        await _broadcast_waitlist(
            "waitlist.settings_updated", user.restaurant_id, {"branch_id": user.branch_id}
        )
        return {**dict(row), "delivery_capabilities": {"browser_push": True, "sms": False, "whatsapp": False}}

    # ─── Display screen data ──────────────────────────────────

    async def get_display_data(self, restaurant_id: UUID, branch_id: Optional[UUID] = None) -> dict:
        user = await self.public_context(restaurant_id, branch_id)
        clause, params = _scope(user)
        async with get_service_connection() as conn:
            settings = await self._get_settings(conn, user) or self._default_settings()
            if not settings["display_screen_enabled"]:
                raise NotFoundError("Waitlist display")
            entries = await conn.fetch(
                f"SELECT customer_name,party_size,status,position,estimated_wait_minutes FROM waitlist_entries "
                f"WHERE {clause} AND status IN ('waiting','notified') ORDER BY position,created_at",
                *params,
            )
        queue = [dict(row) for row in entries]
        waiting = [row for row in queue if row["status"] == "waiting"]
        return {
            "restaurant_id": str(restaurant_id),
            "branch_id": user.branch_id,
            "display_message": settings["display_message"],
            "now_serving": [row for row in queue if row["status"] == "notified"],
            "next_up": waiting[:3],
            "total_waiting": len(waiting),
        }

    # ─── History / Analytics ──────────────────────────────────

    async def get_history(
        self,
        user: UserContext,
        limit: int = 50,
        offset: int = 0,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> dict:
        clause, params = _scope(user)
        conditions = [clause]

        if date_from:
            params.append(_date_bound(date_from))
            conditions.append(f"created_at >= ${len(params)}::timestamptz")
        if date_to:
            params.append(_date_bound(date_to, end=True))
            op = "<" if len(date_to) == 10 else "<="
            conditions.append(f"created_at {op} ${len(params)}::timestamptz")

        where = " AND ".join(conditions)

        async with get_connection() as conn:
            total = await conn.fetchval(f"SELECT COUNT(*) FROM waitlist_entries WHERE {where}", *params)
            params.extend([limit, offset])
            rows = await conn.fetch(
                f"""SELECT * FROM waitlist_entries WHERE {where}
                    ORDER BY created_at DESC
                    LIMIT ${len(params) - 1} OFFSET ${len(params)}""",
                *params,
            )

        return {
            "total": total,
            "entries": [self._format_entry(r) for r in rows],
        }

    async def get_stats(self, user: UserContext) -> dict:
        """Today's waitlist stats."""
        clause, params = _scope(user)
        today_start = datetime.now(_local_zone()).replace(hour=0, minute=0, second=0, microsecond=0)
        params.append(today_start)

        async with get_connection() as conn:
            row = await conn.fetchrow(
                f"""SELECT
                    COUNT(*) as total_today,
                    COUNT(*) FILTER (WHERE status = 'seated') as seated,
                    COUNT(*) FILTER (WHERE status = 'waiting') as waiting,
                    COUNT(*) FILTER (WHERE status = 'notified') as notified,
                    COUNT(*) FILTER (WHERE status = 'skipped') as skipped,
                    COUNT(*) FILTER (WHERE status = 'cancelled') as cancelled,
                    AVG(EXTRACT(EPOCH FROM (seated_at - created_at)) / 60)
                        FILTER (WHERE status = 'seated' AND seated_at IS NOT NULL) as avg_wait_minutes,
                    AVG(party_size) as avg_party_size
                FROM waitlist_entries
                WHERE {clause} AND created_at >= ${len(params)}""",
                *params,
            )

        return {
            "total_today": row["total_today"],
            "seated": row["seated"],
            "waiting": row["waiting"],
            "notified": row["notified"],
            "skipped": row["skipped"],
            "cancelled": row["cancelled"],
            "avg_wait_minutes": round(float(row["avg_wait_minutes"] or 0), 1),
            "avg_party_size": round(float(row["avg_party_size"] or 0), 1),
        }

    # ─── Internal helpers ──────────────────────────────────────

    async def _reposition(self, conn, user):
        clause, params = _scope(user)
        await conn.execute(
            f"WITH ranked AS (SELECT id,row_number() OVER (ORDER BY position,created_at,id) AS pos "
            f"FROM waitlist_entries WHERE {clause} AND status IN ('waiting','notified')) "
            "UPDATE waitlist_entries w SET position=ranked.pos FROM ranked WHERE w.id=ranked.id",
            *params,
        )
        await self._refresh_estimates(conn, user)

    async def refresh_estimates(self, user):
        async with get_transaction() as conn:
            await _lock_queue(conn, user)
            await self._reposition(conn, user)

    async def _refresh_estimates(self, conn, user):
        clause, params = _scope(user)
        settings = await self._get_settings(conn, user) or self._default_settings()
        turnover = settings["avg_turnover_minutes"]
        tables = await conn.fetch(
            f"SELECT id,capacity,is_occupied,status FROM restaurant_tables WHERE {clause} AND is_active",
            *params,
        )
        reservations = set(
            await conn.fetchval(
                f"SELECT array_agg(assigned_table_id) FROM waitlist_entries WHERE {clause} AND status='notified'",
                *params,
            )
            or []
        )
        slots = [
            [
                r["capacity"] or 0,
                (
                    turnover
                    if r["is_occupied"]
                    or r["status"] not in ("blank", "available")
                    or r["id"] in reservations
                    else 0
                ),
            ]
            for r in tables
        ]
        entries = await conn.fetch(
            f"SELECT id,party_size,status FROM waitlist_entries WHERE {clause} AND status IN ('waiting','notified') ORDER BY position,created_at,id",
            *params,
        )
        for entry in entries:
            fits = [slot for slot in slots if slot[0] >= entry["party_size"]]
            eta = None
            if entry["status"] == "notified":
                eta = 0
            elif fits:
                slot = min(fits, key=lambda v: (v[1], v[0]))
                eta = slot[1]
                slot[1] += turnover
            await conn.execute(
                "UPDATE waitlist_entries SET estimated_wait_minutes=$2 WHERE id=$1", entry["id"], eta
            )

    async def _get_settings(self, conn, user: UserContext) -> Optional[dict]:
        _scope(user)
        uid = user.owner_id if user.is_branch_user else user.user_id
        row = await conn.fetchrow(
            "SELECT * FROM waitlist_settings WHERE user_id = $1 AND restaurant_id = $2",
            uid,
            user.restaurant_id,
        )
        return (
            {**dict(row), "delivery_capabilities": {"browser_push": True, "sms": False, "whatsapp": False}}
            if row
            else None
        )

    @staticmethod
    def _default_settings() -> dict:
        return {
            "delivery_capabilities": {"browser_push": True, "sms": False, "whatsapp": False},
            "notify_expiry_minutes": 5,
            "avg_turnover_minutes": 30,
            "sms_enabled": False,
            "whatsapp_enabled": False,
            "display_screen_enabled": False,
            "qr_entry_enabled": True,
            "auto_notify": True,
            "best_fit_enabled": True,
            "display_message": "Welcome! Please wait for your table.",
        }

    async def _log_action(self, conn, restaurant_id, entry_id, action, details, performed_by) -> None:
        await conn.execute(
            """INSERT INTO waitlist_history (restaurant_id, waitlist_entry_id, action, details, performed_by)
               VALUES ($1, $2, $3, $4::jsonb, $5)""",
            restaurant_id,
            entry_id or restaurant_id,
            action,
            __import__("json").dumps(details) if details else None,
            performed_by,
        )

    @staticmethod
    def _format_entry(row) -> dict:
        d = {k: v for k, v in dict(row).items() if k != "push_token_hash"}
        # Convert UUIDs and datetimes for JSON
        for k, v in d.items():
            if isinstance(v, UUID):
                d[k] = str(v)
            elif isinstance(v, datetime):
                d[k] = v.isoformat()
        return d


def _local_zone():
    return ZoneInfo(os.getenv("WAITLIST_TIMEZONE", "Asia/Kolkata"))


def _date_bound(value, end=False):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_local_zone())
        if end and len(value) == 10:
            parsed += timedelta(days=1)
        return parsed
    except (ValueError, TypeError):
        raise ValidationError("Use an ISO date or datetime for history filters")
