"""
Escrow Ledger Service — Phase 2 of the Bittu fintech reconciliation core.

A SECOND immutable, append-only ledger that tracks funds in escrow between
payment-received and settlement-released-to-bank.  Independent of the
Phase 1 merchant_ledger and the legacy journal_entries GL.

Lifecycle
─────────
    payment captured ──► hold_for_payment(...)        ► escrow_hold     CREDIT
                                                       (held_balance += amount)

    cron (T+N elapsed) ► release_due(...)             ► escrow_release  DEBIT
                                                       (held_balance -= amount)

    refund of held    ► refund_hold(...)              ► escrow_refund   DEBIT
    chargeback        ► chargeback_hold(...)          ► escrow_chargeback DEBIT
    age out           ► expire_hold(...)              ► escrow_expired  DEBIT
    admin correction  ► adjust(...)                   ► escrow_adjustment DR/CR

Writing is ONLY via `fn_post_escrow_ledger_entry` (called from `post_entry`
below).  Releases enforce single-use via the `escrow_release_links` PK.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from app.core.auth import UserContext
from app.core.database import get_connection, get_transaction
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

ALLOWED_ESCROW_TYPES = frozenset({
    "escrow_hold",
    "escrow_release",
    "escrow_refund",
    "escrow_chargeback",
    "escrow_expired",
    "escrow_adjustment",
})

DEFAULT_HOLD_DAYS = 1
MAX_HOLD_DAYS = 90


def _merchant_id(user: UserContext) -> str:
    if not user.restaurant_id:
        raise ValidationError("This endpoint requires an active restaurant context.")
    return str(user.restaurant_id)


def _f(value) -> float:
    if value is None:
        return 0.0
    return float(Decimal(str(value)).quantize(Decimal("0.0001")))


def _row_to_dict(row) -> dict[str, Any]:
    if row is None:
        return {}
    md = row["metadata"]
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    return {
        "id":                str(row["id"]),
        "merchant_id":       str(row["merchant_id"]),
        "branch_id":         str(row["branch_id"]) if row["branch_id"] else None,
        "escrow_reference":  row["escrow_reference"],
        "transaction_type":  row["transaction_type"],
        "debit_amount":      _f(row["debit_amount"]),
        "credit_amount":     _f(row["credit_amount"]),
        "balance_after":     _f(row["balance_after"]),
        "currency":          row["currency"],
        "source_type":       row["source_type"],
        "source_id":         str(row["source_id"]) if row["source_id"] else None,
        "payment_id":        str(row["payment_id"]) if row["payment_id"] else None,
        "settlement_id":     str(row["settlement_id"]) if row["settlement_id"] else None,
        "order_id":          str(row["order_id"]) if row["order_id"] else None,
        "bank_reference":    row["bank_reference"],
        "hold_until":        row["hold_until"].isoformat() if row["hold_until"] else None,
        "released_entry_id": str(row["released_entry_id"]) if row["released_entry_id"] else None,
        "idempotency_key":   row["idempotency_key"],
        "metadata":          md or {},
        "created_at":        row["created_at"].isoformat() if row["created_at"] else None,
        "created_by":        str(row["created_by"]) if row["created_by"] else None,
    }


class EscrowService:
    """Service-layer API for the immutable escrow ledger."""

    # ════════════════════════════════════════════════════════════════════
    # GENERIC POST (the only sanctioned write path from Python)
    # ════════════════════════════════════════════════════════════════════
    async def post_entry(
        self,
        *,
        merchant_id: str | UUID,
        transaction_type: str,
        debit_amount: float | Decimal = 0,
        credit_amount: float | Decimal = 0,
        branch_id: Optional[str | UUID] = None,
        currency: str = "INR",
        source_type: Optional[str] = None,
        source_id: Optional[str | UUID] = None,
        payment_id: Optional[str | UUID] = None,
        settlement_id: Optional[str | UUID] = None,
        order_id: Optional[str | UUID] = None,
        bank_reference: Optional[str] = None,
        hold_until: Optional[datetime] = None,
        released_entry_id: Optional[str | UUID] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        created_by: Optional[str | UUID] = None,
        conn=None,
    ) -> dict[str, Any]:
        if transaction_type not in ALLOWED_ESCROW_TYPES:
            raise ValidationError(
                f"unknown transaction_type {transaction_type!r}; "
                f"must be one of {sorted(ALLOWED_ESCROW_TYPES)}"
            )

        d = Decimal(str(debit_amount or 0))
        c = Decimal(str(credit_amount or 0))
        if d < 0 or c < 0:
            raise ValidationError("debit/credit must be non-negative")
        if (d > 0) == (c > 0):
            raise ValidationError(
                "exactly one of debit_amount / credit_amount must be > 0"
            )

        meta_json = json.dumps(metadata or {}, default=str)
        sql = """
            SELECT fn_post_escrow_ledger_entry(
                p_merchant_id        => $1::uuid,
                p_branch_id          => $2::uuid,
                p_transaction_type   => $3::escrow_txn_type,
                p_debit_amount       => $4::numeric,
                p_credit_amount      => $5::numeric,
                p_currency           => $6::char(3),
                p_source_type        => $7::text,
                p_source_id          => $8::uuid,
                p_payment_id         => $9::uuid,
                p_settlement_id      => $10::uuid,
                p_order_id           => $11::uuid,
                p_bank_reference     => $12::text,
                p_hold_until         => $13::timestamptz,
                p_released_entry_id  => $14::uuid,
                p_idempotency_key    => $15::text,
                p_metadata           => $16::jsonb,
                p_created_by         => $17::uuid
            ) AS row
        """
        params = (
            str(merchant_id),
            str(branch_id) if branch_id else None,
            transaction_type,
            d,
            c,
            (currency or "INR").upper(),
            source_type,
            str(source_id) if source_id else None,
            str(payment_id) if payment_id else None,
            str(settlement_id) if settlement_id else None,
            str(order_id) if order_id else None,
            bank_reference,
            hold_until,
            str(released_entry_id) if released_entry_id else None,
            idempotency_key,
            meta_json,
            str(created_by) if created_by else None,
        )

        async def _exec(cx):
            raw = await cx.fetchval(sql, *params)
            if raw is None:
                raise RuntimeError("fn_post_escrow_ledger_entry returned NULL")
            return json.loads(raw) if isinstance(raw, str) else raw

        if conn is not None:
            data = await _exec(conn)
        else:
            async with get_transaction() as cx:
                data = await _exec(cx)

        for k in ("debit_amount", "credit_amount", "balance_after"):
            if k in data and data[k] is not None:
                data[k] = _f(data[k])
        logger.info(
            "escrow_ledger.posted",
            extra={
                "merchant_id": str(merchant_id),
                "escrow_reference": data.get("escrow_reference"),
                "transaction_type": transaction_type,
                "balance_after": data.get("balance_after"),
            },
        )
        return data

    # ════════════════════════════════════════════════════════════════════
    # CONFIG (per-merchant T+N hold window)
    # ════════════════════════════════════════════════════════════════════
    async def get_config(self, merchant_id: str | UUID) -> dict[str, Any]:
        async with get_connection() as c:
            row = await c.fetchrow(
                "SELECT merchant_id, hold_days, enabled, created_at, updated_at "
                "FROM merchant_escrow_config WHERE merchant_id = $1::uuid",
                str(merchant_id),
            )
        if row is None:
            return {
                "merchant_id": str(merchant_id),
                "hold_days":   DEFAULT_HOLD_DAYS,
                "enabled":     True,
                "is_default":  True,
            }
        return {
            "merchant_id": str(row["merchant_id"]),
            "hold_days":   int(row["hold_days"]),
            "enabled":     bool(row["enabled"]),
            "is_default":  False,
            "created_at":  row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at":  row["updated_at"].isoformat() if row["updated_at"] else None,
        }

    async def set_config(
        self,
        merchant_id: str | UUID,
        *,
        hold_days: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> dict[str, Any]:
        if hold_days is not None:
            if hold_days < 0 or hold_days > MAX_HOLD_DAYS:
                raise ValidationError(
                    f"hold_days must be between 0 and {MAX_HOLD_DAYS}"
                )
        async with get_transaction() as cx:
            await cx.execute(
                """
                INSERT INTO merchant_escrow_config (merchant_id, hold_days, enabled)
                VALUES ($1::uuid, $2, $3)
                ON CONFLICT (merchant_id) DO UPDATE
                  SET hold_days  = COALESCE($2, merchant_escrow_config.hold_days),
                      enabled    = COALESCE($3, merchant_escrow_config.enabled),
                      updated_at = now()
                """,
                str(merchant_id),
                hold_days if hold_days is not None else DEFAULT_HOLD_DAYS,
                enabled if enabled is not None else True,
            )
        return await self.get_config(merchant_id)

    async def _resolve_hold_days(self, merchant_id: str | UUID, conn=None) -> int:
        sql = ("SELECT hold_days FROM merchant_escrow_config "
               "WHERE merchant_id = $1::uuid")
        if conn is not None:
            row = await conn.fetchval(sql, str(merchant_id))
        else:
            async with get_connection() as cx:
                row = await cx.fetchval(sql, str(merchant_id))
        return int(row) if row is not None else DEFAULT_HOLD_DAYS

    # ════════════════════════════════════════════════════════════════════
    # HOLD (called from payment-completed integration)
    # ════════════════════════════════════════════════════════════════════
    async def hold_for_payment(
        self,
        *,
        merchant_id: str | UUID,
        payment_id: str | UUID,
        amount: float | Decimal,
        branch_id: Optional[str | UUID] = None,
        order_id: Optional[str | UUID] = None,
        currency: str = "INR",
        hold_days: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
        created_by: Optional[str | UUID] = None,
        conn=None,
    ) -> dict[str, Any]:
        """
        Post an escrow_hold CREDIT for a captured payment.

        Idempotent on `escrow_hold:{payment_id}`.  Hold window resolved
        from `merchant_escrow_config.hold_days` (or DEFAULT_HOLD_DAYS).
        """
        days = hold_days if hold_days is not None else \
            await self._resolve_hold_days(merchant_id, conn=conn)
        hold_until = datetime.now(timezone.utc) + timedelta(days=days)
        return await self.post_entry(
            merchant_id=merchant_id,
            branch_id=branch_id,
            transaction_type="escrow_hold",
            credit_amount=amount,
            currency=currency,
            source_type="payment",
            payment_id=payment_id,
            order_id=order_id,
            hold_until=hold_until,
            idempotency_key=f"escrow_hold:{payment_id}",
            metadata={"hold_days": days, **(metadata or {})},
            created_by=created_by,
            conn=conn,
        )

    # ════════════════════════════════════════════════════════════════════
    # RELEASE (cron path + direct API)
    # ════════════════════════════════════════════════════════════════════
    async def release_hold(
        self,
        *,
        merchant_id: str | UUID,
        hold_entry_id: str | UUID,
        amount: float | Decimal,
        settlement_id: Optional[str | UUID] = None,
        bank_reference: Optional[str] = None,
        reason: str = "auto_release",
        metadata: Optional[dict[str, Any]] = None,
        created_by: Optional[str | UUID] = None,
        conn=None,
    ) -> dict[str, Any]:
        """
        Release a previously-held escrow entry.  Single-release contract is
        enforced by `escrow_release_links` PK — calling twice with the same
        hold_entry_id raises a unique-violation in postgres.

        Idempotent at the helper level via `escrow_release:{hold_entry_id}`.
        """
        return await self.post_entry(
            merchant_id=merchant_id,
            transaction_type="escrow_release",
            debit_amount=amount,
            source_type="release",
            settlement_id=settlement_id,
            bank_reference=bank_reference,
            released_entry_id=hold_entry_id,
            idempotency_key=f"escrow_release:{hold_entry_id}",
            metadata={"reason": reason, **(metadata or {})},
            created_by=created_by,
            conn=conn,
        )

    async def list_due_for_release(
        self,
        *,
        now: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read-only preview of holds that the next cron tick would release."""
        when = now or datetime.now(timezone.utc)
        async with get_connection() as c:
            rows = await c.fetch(
                "SELECT * FROM fn_select_due_escrow_holds($1::timestamptz, $2)",
                when, int(limit),
            )
        return [
            {
                "hold_id":       str(r["hold_id"]),
                "merchant_id":   str(r["merchant_id"]),
                "branch_id":     str(r["branch_id"]) if r["branch_id"] else None,
                "currency":      r["currency"],
                "credit_amount": _f(r["credit_amount"]),
                "payment_id":    str(r["payment_id"]) if r["payment_id"] else None,
                "order_id":      str(r["order_id"]) if r["order_id"] else None,
                "hold_until":    r["hold_until"].isoformat() if r["hold_until"] else None,
                "created_at":    r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]

    async def release_due(
        self,
        *,
        now: Optional[datetime] = None,
        limit: int = 100,
        actor_id: Optional[str | UUID] = None,
    ) -> dict[str, Any]:
        """
        The cron job's entry point.  Releases up to `limit` due holds.

        Each release runs in its own transaction so a single failure cannot
        block the rest.  Returns aggregate counts.
        """
        due = await self.list_due_for_release(now=now, limit=limit)
        released = 0
        failed = 0
        errors: list[dict[str, Any]] = []
        for h in due:
            try:
                await self.release_hold(
                    merchant_id=h["merchant_id"],
                    hold_entry_id=h["hold_id"],
                    amount=h["credit_amount"],
                    reason="auto_release_cron",
                    metadata={
                        "original_hold_until": h["hold_until"],
                        "payment_id":          h["payment_id"],
                    },
                    created_by=actor_id,
                )
                released += 1
            except Exception as exc:
                failed += 1
                errors.append({"hold_id": h["hold_id"], "error": str(exc)})
                logger.error(
                    "escrow_ledger.auto_release.failed",
                    extra={"hold_id": h["hold_id"], "error": str(exc)},
                )
        logger.info(
            "escrow_ledger.auto_release.summary",
            extra={"released": released, "failed": failed, "considered": len(due)},
        )
        return {
            "considered": len(due),
            "released":   released,
            "failed":     failed,
            "errors":     errors[:20],
        }

    # ════════════════════════════════════════════════════════════════════
    # READ
    # ════════════════════════════════════════════════════════════════════
    async def get_balance(
        self, user: UserContext, *, currency: str = "INR"
    ) -> dict[str, Any]:
        mid = _merchant_id(user)
        currency = (currency or "INR").upper()
        async with get_connection() as c:
            row = await c.fetchrow(
                "SELECT held_balance, last_entry_id, last_posted_at, version "
                "FROM escrow_balance_locks "
                "WHERE merchant_id = $1::uuid AND currency = $2",
                mid, currency,
            )
            open_holds = await c.fetchval(
                """
                SELECT COALESCE(SUM(h.credit_amount), 0)
                  FROM escrow_ledger h
                  LEFT JOIN escrow_release_links rl
                         ON rl.merchant_id   = h.merchant_id
                        AND rl.hold_entry_id = h.id
                 WHERE h.merchant_id = $1::uuid
                   AND h.currency    = $2
                   AND h.transaction_type = 'escrow_hold'
                   AND rl.hold_entry_id IS NULL
                """,
                mid, currency,
            )
        if row is None:
            return {
                "merchant_id":      mid,
                "currency":         currency,
                "held_balance":     0.0,
                "open_holds_total": _f(open_holds),
                "last_entry_id":    None,
                "last_posted_at":   None,
                "version":          0,
            }
        return {
            "merchant_id":      mid,
            "currency":         currency,
            "held_balance":     _f(row["held_balance"]),
            "open_holds_total": _f(open_holds),
            "last_entry_id":    str(row["last_entry_id"]) if row["last_entry_id"] else None,
            "last_posted_at":   row["last_posted_at"].isoformat() if row["last_posted_at"] else None,
            "version":          int(row["version"]),
        }

    async def list_entries(
        self,
        user: UserContext,
        *,
        transaction_type: Optional[str] = None,
        payment_id:       Optional[str] = None,
        settlement_id:    Optional[str] = None,
        order_id:         Optional[str] = None,
        from_date:        Optional[str] = None,
        to_date:          Optional[str] = None,
        currency:         str = "INR",
        limit:            int = 50,
        cursor:           Optional[str] = None,
    ) -> dict[str, Any]:
        mid = _merchant_id(user)
        limit = max(1, min(int(limit), 200))

        clauses: list[str] = ["merchant_id = $1::uuid", "currency = $2"]
        params: list[Any] = [mid, (currency or "INR").upper()]

        def _add(clause: str, value: Any) -> None:
            params.append(value)
            clauses.append(clause.replace("?", f"${len(params)}"))

        if transaction_type:
            if transaction_type not in ALLOWED_ESCROW_TYPES:
                raise ValidationError(f"unknown transaction_type {transaction_type!r}")
            _add("transaction_type = ?::escrow_txn_type", transaction_type)
        if payment_id:
            _add("payment_id = ?::uuid", payment_id)
        if settlement_id:
            _add("settlement_id = ?::uuid", settlement_id)
        if order_id:
            _add("order_id = ?::uuid", order_id)
        if from_date:
            _add("created_at >= ?::timestamptz", from_date)
        if to_date:
            _add("created_at <= ?::timestamptz", to_date)

        if user.is_branch_user and user.branch_id:
            _add("branch_id = ?::uuid", str(user.branch_id))

        if cursor:
            try:
                cur_ts, cur_id = cursor.split("|", 1)
            except ValueError as e:
                raise ValidationError("invalid cursor") from e
            params.append(cur_ts)
            params.append(cur_id)
            clauses.append(
                f"(created_at, id) < (${len(params) - 1}::timestamptz, ${len(params)}::uuid)"
            )

        params.append(limit + 1)
        sql = f"""
            SELECT id, merchant_id, branch_id, escrow_reference, transaction_type,
                   debit_amount, credit_amount, balance_after, currency,
                   source_type, source_id, payment_id, settlement_id, order_id,
                   bank_reference, hold_until, released_entry_id,
                   idempotency_key, metadata, created_at, created_by
              FROM escrow_ledger
             WHERE {' AND '.join(clauses)}
             ORDER BY created_at DESC, id DESC
             LIMIT ${len(params)}
        """
        async with get_connection() as c:
            rows = await c.fetch(sql, *params)

        items = [_row_to_dict(r) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = f"{last['created_at'].isoformat()}|{last['id']}"
        return {"items": items, "next_cursor": next_cursor, "count": len(items)}

    async def get_entry(self, user: UserContext, entry_id: str) -> dict[str, Any]:
        mid = _merchant_id(user)
        async with get_connection() as c:
            row = await c.fetchrow(
                """
                SELECT id, merchant_id, branch_id, escrow_reference, transaction_type,
                       debit_amount, credit_amount, balance_after, currency,
                       source_type, source_id, payment_id, settlement_id, order_id,
                       bank_reference, hold_until, released_entry_id,
                       idempotency_key, metadata, created_at, created_by
                  FROM escrow_ledger
                 WHERE id = $1::uuid AND merchant_id = $2::uuid
                """,
                entry_id, mid,
            )
        if row is None:
            raise NotFoundError("escrow_ledger entry", entry_id)
        return _row_to_dict(row)

    async def verify_consistency(
        self, user: UserContext, *, currency: str = "INR"
    ) -> dict[str, Any]:
        mid = _merchant_id(user)
        async with get_connection() as c:
            raw = await c.fetchval(
                "SELECT fn_check_escrow_consistency($1::uuid, $2)",
                mid, (currency or "INR").upper(),
            )
        return json.loads(raw) if isinstance(raw, str) else raw


escrow_service = EscrowService()
