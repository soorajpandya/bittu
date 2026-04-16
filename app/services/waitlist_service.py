"""
Smart Waitlist Service.

Unified queue engine with best-fit table allocation, notification tracking,
and support for both staff (POS) and customer (QR) entry points.
"""
import math
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import structlog

from app.core.database import get_connection
from app.core.tenant import tenant_where_clause, tenant_insert_fields
from app.core.auth import UserContext
from app.core.exceptions import NotFoundError, ConflictError

logger = structlog.get_logger(__name__)


class WaitlistService:

    # ─── Add to waitlist ───────────────────────────────────────

    async def add_entry(
        self,
        user: UserContext,
        customer_name: str,
        party_size: int,
        phone: Optional[str] = None,
        source: str = "staff",
        notes: Optional[str] = None,
    ) -> dict:
        """Add a customer to the waitlist. Works for both staff and QR entry."""
        tenant = tenant_insert_fields(user)

        async with get_connection() as conn:
            # Duplicate check: same phone already waiting at this restaurant
            if phone:
                dup = await conn.fetchrow(
                    """SELECT id FROM waitlist_entries
                       WHERE user_id = $1 AND phone = $2 AND status IN ('waiting', 'notified')
                       AND ($3::uuid IS NULL OR restaurant_id = $3)""",
                    tenant["user_id"], phone, user.restaurant_id,
                )
                if dup:
                    raise ConflictError(f"Customer with phone {phone} is already on the waitlist")

            # Get next position
            max_pos = await conn.fetchval(
                """SELECT COALESCE(MAX(position), 0) FROM waitlist_entries
                   WHERE user_id = $1 AND status IN ('waiting', 'notified')
                   AND ($2::uuid IS NULL OR restaurant_id = $2)""",
                tenant["user_id"], user.restaurant_id,
            )
            position = max_pos + 1

            # Estimate wait time
            est_minutes = await self._estimate_wait(conn, tenant["user_id"], user.restaurant_id, position, party_size)

            row = await conn.fetchrow(
                """INSERT INTO waitlist_entries
                   (restaurant_id, branch_id, user_id, customer_name, phone,
                    party_size, source, status, position, estimated_wait_minutes, notes)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, 'waiting', $8, $9, $10)
                   RETURNING *""",
                user.restaurant_id, tenant.get("branch_id"), tenant["user_id"],
                customer_name, phone, party_size, source, position, est_minutes, notes,
            )

            # Audit log
            await self._log_action(conn, user.restaurant_id, row["id"], "added",
                                   {"source": source, "party_size": party_size},
                                   tenant["user_id"] if source == "staff" else "customer")

        logger.info("waitlist_entry_added", id=str(row["id"]), name=customer_name, position=position)
        return self._format_entry(row)

    # ─── Get active queue ──────────────────────────────────────

    async def get_queue(
        self,
        user: UserContext,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Get current waitlist queue for the restaurant."""
        clause, params = tenant_where_clause(user)

        conditions = [clause]
        if status:
            params.append(status)
            conditions.append(f"status = ${len(params)}")
        else:
            # Default: show active entries
            conditions.append("status IN ('waiting', 'notified')")

        where = " AND ".join(conditions)

        async with get_connection() as conn:
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM waitlist_entries WHERE {where}", *params
            )

            params.extend([limit, offset])
            rows = await conn.fetch(
                f"""SELECT w.*, rt.table_number, rt.capacity as table_capacity
                    FROM waitlist_entries w
                    LEFT JOIN restaurant_tables rt ON rt.id = w.assigned_table_id
                    WHERE {where}
                    ORDER BY w.position ASC
                    LIMIT ${len(params) - 1} OFFSET ${len(params)}""",
                *params,
            )

        return {
            "total": total,
            "entries": [self._format_entry(r) for r in rows],
        }

    # ─── Get single entry (public — for QR customers) ─────────

    async def get_entry_status(self, entry_id: UUID) -> Optional[dict]:
        """Get waitlist entry status. Used by QR customer page — no auth needed."""
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """SELECT w.*, rt.table_number
                   FROM waitlist_entries w
                   LEFT JOIN restaurant_tables rt ON rt.id = w.assigned_table_id
                   WHERE w.id = $1""",
                entry_id,
            )
        if not row:
            return None
        return self._format_entry(row)

    # ─── Notify next (best-fit or FIFO) ───────────────────────

    async def notify_next(self, user: UserContext, table_id: Optional[UUID] = None) -> Optional[dict]:
        """
        Find the best-fit customer for available table(s) and mark as notified.
        If table_id is given, match against that specific table.
        Otherwise, check all vacant tables.
        """
        clause, params = tenant_where_clause(user)

        async with get_connection() as conn:
            # Get settings
            settings = await self._get_settings(conn, user)
            expiry_minutes = settings["notify_expiry_minutes"] if settings else 5
            best_fit = settings["best_fit_enabled"] if settings else True

            # Get available table(s)
            if table_id:
                tables = await conn.fetch(
                    f"""SELECT id, table_number, capacity FROM restaurant_tables
                        WHERE {clause} AND id = ${len(params) + 1}
                        AND status = 'blank' AND is_active = true""",
                    *params, table_id,
                )
            else:
                tables = await conn.fetch(
                    f"""SELECT id, table_number, capacity FROM restaurant_tables
                        WHERE {clause} AND status = 'blank' AND is_active = true
                        ORDER BY capacity ASC""",
                    *params,
                )

            if not tables:
                return None

            # Get waiting entries
            w_params = [user.user_id] if not user.is_branch_user else [user.owner_id]
            waiting = await conn.fetch(
                """SELECT * FROM waitlist_entries
                   WHERE user_id = $1 AND status = 'waiting'
                   ORDER BY position ASC""",
                *w_params,
            )

            if not waiting:
                return None

            # Best-fit matching: for each table, find best-fit customer
            matched = None
            matched_table = None

            for table in tables:
                cap = table["capacity"]
                if best_fit:
                    # Find closest party_size <= capacity, preferring exact match
                    best = None
                    best_diff = float("inf")
                    for entry in waiting:
                        if entry["party_size"] <= cap:
                            diff = cap - entry["party_size"]
                            if diff < best_diff:
                                best = entry
                                best_diff = diff
                                if diff == 0:
                                    break  # perfect match
                    if best:
                        matched = best
                        matched_table = table
                        break
                else:
                    # FIFO: just take first that fits
                    for entry in waiting:
                        if entry["party_size"] <= cap:
                            matched = entry
                            matched_table = table
                            break
                    if matched:
                        break

            if not matched or not matched_table:
                return None

            # Mark as notified
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes)
            row = await conn.fetchrow(
                """UPDATE waitlist_entries
                   SET status = 'notified', notified_at = now(), expires_at = $1,
                       assigned_table_id = $2
                   WHERE id = $3 RETURNING *""",
                expires_at, matched_table["id"], matched["id"],
            )

            await self._log_action(conn, user.restaurant_id, matched["id"], "notified",
                                   {"table": matched_table["table_number"],
                                    "expires_at": expires_at.isoformat()},
                                   user.user_id)

        logger.info("waitlist_notified",
                     entry_id=str(matched["id"]),
                     table=matched_table["table_number"],
                     party_size=matched["party_size"])

        result = self._format_entry(row)
        result["assigned_table_number"] = matched_table["table_number"]
        return result

    # ─── Seat customer ─────────────────────────────────────────

    async def seat_customer(self, user: UserContext, entry_id: UUID) -> dict:
        """Mark customer as seated and update table status."""
        clause, params = tenant_where_clause(user)
        params.append(entry_id)

        async with get_connection() as conn:
            entry = await conn.fetchrow(
                f"""SELECT * FROM waitlist_entries
                    WHERE {clause} AND id = ${len(params)}
                    AND status IN ('waiting', 'notified')""",
                *params,
            )
            if not entry:
                raise NotFoundError("Waitlist entry", str(entry_id))

            # Update entry
            row = await conn.fetchrow(
                """UPDATE waitlist_entries
                   SET status = 'seated', seated_at = now()
                   WHERE id = $1 RETURNING *""",
                entry_id,
            )

            # Mark table as occupied if assigned
            if entry["assigned_table_id"]:
                await conn.execute(
                    """UPDATE restaurant_tables
                       SET status = 'running', is_occupied = true, occupied_since = now()
                       WHERE id = $1""",
                    entry["assigned_table_id"],
                )

            await self._log_action(conn, user.restaurant_id, entry_id, "seated",
                                   {"table_id": str(entry["assigned_table_id"]) if entry["assigned_table_id"] else None},
                                   user.user_id)

            # Recalculate positions for remaining waiting entries
            await self._reposition(conn, entry["user_id"], entry["restaurant_id"])

        logger.info("waitlist_seated", entry_id=str(entry_id))
        return self._format_entry(row)

    # ─── Skip customer ─────────────────────────────────────────

    async def skip_customer(self, user: UserContext, entry_id: UUID, reason: str = "no_show") -> dict:
        """Skip a customer (no-show or manual skip)."""
        clause, params = tenant_where_clause(user)
        params.append(entry_id)

        async with get_connection() as conn:
            entry = await conn.fetchrow(
                f"""SELECT * FROM waitlist_entries
                    WHERE {clause} AND id = ${len(params)}
                    AND status IN ('waiting', 'notified')""",
                *params,
            )
            if not entry:
                raise NotFoundError("Waitlist entry", str(entry_id))

            row = await conn.fetchrow(
                """UPDATE waitlist_entries
                   SET status = 'skipped', assigned_table_id = NULL
                   WHERE id = $1 RETURNING *""",
                entry_id,
            )

            await self._log_action(conn, user.restaurant_id, entry_id, "skipped",
                                   {"reason": reason}, user.user_id)

            await self._reposition(conn, entry["user_id"], entry["restaurant_id"])

        logger.info("waitlist_skipped", entry_id=str(entry_id), reason=reason)
        return self._format_entry(row)

    # ─── Cancel (by customer or staff) ─────────────────────────

    async def cancel_entry(self, user: UserContext, entry_id: UUID) -> dict:
        """Cancel a waitlist entry."""
        clause, params = tenant_where_clause(user)
        params.append(entry_id)

        async with get_connection() as conn:
            entry = await conn.fetchrow(
                f"""SELECT * FROM waitlist_entries
                    WHERE {clause} AND id = ${len(params)}
                    AND status IN ('waiting', 'notified')""",
                *params,
            )
            if not entry:
                raise NotFoundError("Waitlist entry", str(entry_id))

            row = await conn.fetchrow(
                """UPDATE waitlist_entries
                   SET status = 'cancelled', assigned_table_id = NULL
                   WHERE id = $1 RETURNING *""",
                entry_id,
            )

            await self._log_action(conn, user.restaurant_id, entry_id, "cancelled",
                                   None, user.user_id)

            await self._reposition(conn, entry["user_id"], entry["restaurant_id"])

        logger.info("waitlist_cancelled", entry_id=str(entry_id))
        return self._format_entry(row)

    # ─── Reorder (admin drag & drop) ──────────────────────────

    async def reorder(self, user: UserContext, ordered_ids: list[UUID]) -> list[dict]:
        """Reorder the queue (admin override). Accepts ordered list of entry IDs."""
        clause, params = tenant_where_clause(user)

        async with get_connection() as conn:
            results = []
            for i, eid in enumerate(ordered_ids, start=1):
                row = await conn.fetchrow(
                    f"""UPDATE waitlist_entries SET position = $1
                        WHERE id = $2 AND {clause} AND status IN ('waiting', 'notified')
                        RETURNING *""",
                    i, eid, *params,
                )
                if row:
                    results.append(self._format_entry(row))

            await self._log_action(conn, user.restaurant_id, None, "reordered",
                                   {"new_order": [str(eid) for eid in ordered_ids]},
                                   user.user_id)

        logger.info("waitlist_reordered", count=len(results))
        return results

    # ─── Check & expire overdue notifications ──────────────────

    async def expire_overdue(self, user: UserContext) -> list[dict]:
        """Check for notified entries past their expiry. Mark as skipped."""
        clause, params = tenant_where_clause(user)

        async with get_connection() as conn:
            overdue = await conn.fetch(
                f"""SELECT * FROM waitlist_entries
                    WHERE {clause} AND status = 'notified'
                    AND expires_at IS NOT NULL AND expires_at < now()""",
                *params,
            )

            results = []
            for entry in overdue:
                row = await conn.fetchrow(
                    """UPDATE waitlist_entries
                       SET status = 'skipped', assigned_table_id = NULL
                       WHERE id = $1 RETURNING *""",
                    entry["id"],
                )
                results.append(self._format_entry(row))

                await self._log_action(conn, user.restaurant_id, entry["id"], "skipped",
                                       {"reason": "expired"}, "system")

            if overdue:
                await self._reposition(conn,
                                       user.owner_id if user.is_branch_user else user.user_id,
                                       user.restaurant_id)

        if results:
            logger.info("waitlist_expired", count=len(results))
        return results

    # ─── Table freed → auto-notify ────────────────────────────

    async def on_table_freed(self, user: UserContext, table_id: UUID) -> Optional[dict]:
        """
        Called when a table becomes vacant.
        If auto_notify is ON, finds best-fit and notifies automatically.
        """
        async with get_connection() as conn:
            settings = await self._get_settings(conn, user)
            if not settings or not settings.get("auto_notify", True):
                return None

        return await self.notify_next(user, table_id=table_id)

    # ─── Settings CRUD ────────────────────────────────────────

    async def get_settings(self, user: UserContext) -> dict:
        async with get_connection() as conn:
            return await self._get_settings(conn, user) or self._default_settings()

    async def update_settings(self, user: UserContext, data: dict) -> dict:
        tenant = tenant_insert_fields(user)

        allowed = [
            "notify_expiry_minutes", "avg_turnover_minutes", "sms_enabled",
            "whatsapp_enabled", "display_screen_enabled", "qr_entry_enabled",
            "auto_notify", "best_fit_enabled", "display_message",
        ]
        fields = {k: v for k, v in data.items() if k in allowed and v is not None}
        if not fields:
            return await self.get_settings(user)

        async with get_connection() as conn:
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

        return dict(row)

    # ─── Display screen data ──────────────────────────────────

    async def get_display_data(self, restaurant_id: UUID) -> dict:
        """Public endpoint for display screen. No auth — keyed by restaurant_id."""
        async with get_connection() as conn:
            entries = await conn.fetch(
                """SELECT customer_name, party_size, status, position, estimated_wait_minutes
                   FROM waitlist_entries
                   WHERE restaurant_id = $1 AND status IN ('waiting', 'notified')
                   ORDER BY position ASC LIMIT 20""",
                restaurant_id,
            )

            settings = await conn.fetchrow(
                "SELECT display_message FROM waitlist_settings WHERE restaurant_id = $1",
                restaurant_id,
            )

        queue = [dict(r) for r in entries]
        now_serving = [e for e in queue if e["status"] == "notified"]
        waiting = [e for e in queue if e["status"] == "waiting"]

        return {
            "restaurant_id": str(restaurant_id),
            "display_message": settings["display_message"] if settings else "Welcome! Please wait for your table.",
            "now_serving": now_serving,
            "next_up": waiting[:3],
            "total_waiting": len(waiting),
        }

    # ─── History / Analytics ──────────────────────────────────

    async def get_history(
        self, user: UserContext, limit: int = 50, offset: int = 0,
        date_from: Optional[str] = None, date_to: Optional[str] = None,
    ) -> dict:
        clause, params = tenant_where_clause(user)
        conditions = [clause]

        if date_from:
            params.append(date_from)
            conditions.append(f"created_at >= ${len(params)}::timestamptz")
        if date_to:
            params.append(date_to)
            conditions.append(f"created_at <= ${len(params)}::timestamptz")

        where = " AND ".join(conditions)

        async with get_connection() as conn:
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM waitlist_entries WHERE {where}", *params
            )
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
        clause, params = tenant_where_clause(user)
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
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

    async def _estimate_wait(self, conn, user_id: str, restaurant_id, position: int, party_size: int) -> int:
        """Estimate wait time based on turnover and queue ahead."""
        settings = await conn.fetchrow(
            "SELECT avg_turnover_minutes FROM waitlist_settings WHERE user_id = $1 AND restaurant_id = $2",
            user_id, restaurant_id,
        )
        turnover = settings["avg_turnover_minutes"] if settings else 30

        # Count tables that can fit this party
        fitting_tables = await conn.fetchval(
            """SELECT COUNT(*) FROM restaurant_tables
               WHERE user_id = $1 AND is_active = true AND capacity >= $2
               AND ($3::uuid IS NULL OR restaurant_id = $3)""",
            user_id, party_size, restaurant_id,
        )
        fitting_tables = max(fitting_tables, 1)

        # People ahead in queue with same or smaller party size
        ahead = await conn.fetchval(
            """SELECT COUNT(*) FROM waitlist_entries
               WHERE user_id = $1 AND status = 'waiting' AND party_size <= $2
               AND ($3::uuid IS NULL OR restaurant_id = $3)""",
            user_id, party_size, restaurant_id,
        )

        # Estimate: (people ahead / fitting tables) * turnover time
        return max(5, math.ceil((ahead / fitting_tables) * turnover))

    async def _reposition(self, conn, user_id: str, restaurant_id) -> None:
        """Recalculate positions for all active waiting entries."""
        rows = await conn.fetch(
            """SELECT id FROM waitlist_entries
               WHERE user_id = $1 AND status IN ('waiting', 'notified')
               AND ($2::uuid IS NULL OR restaurant_id = $2)
               ORDER BY position ASC, created_at ASC""",
            user_id, restaurant_id,
        )
        for i, row in enumerate(rows, start=1):
            await conn.execute(
                "UPDATE waitlist_entries SET position = $1 WHERE id = $2",
                i, row["id"],
            )

    async def _get_settings(self, conn, user: UserContext) -> Optional[dict]:
        uid = user.owner_id if user.is_branch_user else user.user_id
        row = await conn.fetchrow(
            "SELECT * FROM waitlist_settings WHERE user_id = $1 AND restaurant_id = $2",
            uid, user.restaurant_id,
        )
        return dict(row) if row else None

    @staticmethod
    def _default_settings() -> dict:
        return {
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
            restaurant_id, entry_id or restaurant_id, action,
            __import__("json").dumps(details) if details else None,
            performed_by,
        )

    @staticmethod
    def _format_entry(row) -> dict:
        d = dict(row)
        # Convert UUIDs and datetimes for JSON
        for k, v in d.items():
            if isinstance(v, UUID):
                d[k] = str(v)
            elif isinstance(v, datetime):
                d[k] = v.isoformat()
        return d
