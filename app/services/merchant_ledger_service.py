"""
Merchant Ledger Service — Phase 1 of the Bittu fintech reconciliation core.

The merchant ledger is an APPEND-ONLY, immutable record of every money
movement for a merchant. It runs in PARALLEL with the existing
journal_entries / journal_lines double-entry GL — it does not replace it.

Writing to the ledger
─────────────────────
Use `MerchantLedgerService.post_entry(...)`. It calls the SQL function
`fn_post_merchant_ledger_entry`, which:

  * locks the per-(merchant, currency) balance row,
  * computes balance_after deterministically,
  * allocates a stable per-merchant `ledger_reference`,
  * dedupes by `idempotency_key` (returns the existing row on replay),
  * inserts an immutable row.

The function is the ONLY supported write path. Direct INSERT works but
bypasses the per-merchant lock and will race under concurrency.

Reading from the ledger
───────────────────────
Use `get_balance`, `list_entries`, `get_entry`, `verify_consistency`.

Tenant scoping uses `restaurant_id` as the merchant identifier (consistent
with `merchant_wallet_service`).
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from app.core.auth import UserContext
from app.core.database import get_connection, get_transaction
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

# Allowed transaction types (mirrors merchant_ledger_txn_type enum).
ALLOWED_TXN_TYPES = frozenset({
    "opening_balance",
    "payment_received",
    "settlement_initiated",
    "settlement_completed",
    "settlement_reversed",
    "fee_deduction",
    "gst_deduction",
    "refund",
    "chargeback",
    "payout_failure",
    "adjustment",
    "reserve_hold",
    "reserve_release",
    "manual_credit",
    "manual_debit",
})


def _merchant_id(user: UserContext) -> str:
    """Resolve the merchant (restaurant) id from the user context."""
    if not user.restaurant_id:
        raise ValidationError(
            "This endpoint requires an active restaurant context."
        )
    return str(user.restaurant_id)


def _f(value) -> float:
    """Decimal/None → float (4dp) for JSON serialisation."""
    if value is None:
        return 0.0
    return float(Decimal(str(value)).quantize(Decimal("0.0001")))


def _parse_uuid(value: str, field_name: str) -> UUID:
    """Parse a UUID string and raise API validation errors on bad input."""
    try:
        return UUID(str(value))
    except Exception as e:
        raise ValidationError(f"invalid {field_name}") from e


def _row_to_dict(row) -> dict[str, Any]:
    """Map a merchant_ledger asyncpg.Record to a JSON-safe dict."""
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
        "ledger_reference":  row["ledger_reference"],
        "transaction_type":  row["transaction_type"],
        "debit_amount":      _f(row["debit_amount"]),
        "credit_amount":     _f(row["credit_amount"]),
        "balance_after":     _f(row["balance_after"]),
        "currency":          row["currency"],
        "source_type":       row["source_type"],
        "source_id":         str(row["source_id"]) if row["source_id"] else None,
        "settlement_id":     str(row["settlement_id"]) if row["settlement_id"] else None,
        "payment_id":        str(row["payment_id"]) if row["payment_id"] else None,
        "order_id":          str(row["order_id"]) if row["order_id"] else None,
        "bank_reference":    row["bank_reference"],
        "utr_number":        row["utr_number"],
        "idempotency_key":   row["idempotency_key"],
        "metadata":          md or {},
        "created_at":        row["created_at"].isoformat() if row["created_at"] else None,
        "created_by":        str(row["created_by"]) if row["created_by"] else None,
    }


class MerchantLedgerService:
    """Service-layer API for the immutable merchant ledger."""

    # ── Write ───────────────────────────────────────────────────────────
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
        settlement_id: Optional[str | UUID] = None,
        payment_id: Optional[str | UUID] = None,
        order_id: Optional[str | UUID] = None,
        bank_reference: Optional[str] = None,
        utr_number: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        created_by: Optional[str | UUID] = None,
        conn=None,
    ) -> dict[str, Any]:
        """
        Post a single immutable ledger entry.

        Atomic + idempotent + race-safe via the SQL posting function.
        Pass `conn=` to participate in an outer transaction; otherwise a
        fresh connection is acquired.

        Returns the inserted (or, on idempotency hit, the pre-existing) row.
        """
        if transaction_type not in ALLOWED_TXN_TYPES:
            raise ValidationError(
                f"unknown transaction_type {transaction_type!r}; "
                f"must be one of {sorted(ALLOWED_TXN_TYPES)}"
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
            SELECT fn_post_merchant_ledger_entry(
                p_merchant_id      => $1::uuid,
                p_branch_id        => $2::uuid,
                p_transaction_type => $3::merchant_ledger_txn_type,
                p_debit_amount     => $4::numeric,
                p_credit_amount    => $5::numeric,
                p_currency         => $6::char(3),
                p_source_type      => $7::text,
                p_source_id        => $8::uuid,
                p_settlement_id    => $9::uuid,
                p_payment_id       => $10::uuid,
                p_order_id         => $11::uuid,
                p_bank_reference   => $12::text,
                p_utr_number       => $13::text,
                p_idempotency_key  => $14::text,
                p_metadata         => $15::jsonb,
                p_created_by       => $16::uuid
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
            str(settlement_id) if settlement_id else None,
            str(payment_id) if payment_id else None,
            str(order_id) if order_id else None,
            bank_reference,
            utr_number,
            idempotency_key,
            meta_json,
            str(created_by) if created_by else None,
        )

        async def _exec(c):
            raw = await c.fetchval(sql, *params)
            if raw is None:
                raise RuntimeError("fn_post_merchant_ledger_entry returned NULL")
            return json.loads(raw) if isinstance(raw, str) else raw

        if conn is not None:
            data = await _exec(conn)
        else:
            async with get_transaction() as cx:
                data = await _exec(cx)

        # Normalise float fields the function returned as text in JSONB.
        for k in ("debit_amount", "credit_amount", "balance_after"):
            if k in data and data[k] is not None:
                data[k] = _f(data[k])
        logger.info(
            "merchant_ledger.posted",
            extra={
                "merchant_id": str(merchant_id),
                "ledger_reference": data.get("ledger_reference"),
                "transaction_type": transaction_type,
                "balance_after": data.get("balance_after"),
            },
        )
        return data

    # ── Read ────────────────────────────────────────────────────────────
    async def get_balance(
        self,
        user: UserContext,
        *,
        currency: str = "INR",
    ) -> dict[str, Any]:
        """Current running balance for the caller's merchant + currency."""
        mid = _merchant_id(user)
        currency = (currency or "INR").upper()
        async with get_connection() as c:
            row = await c.fetchrow(
                """
                SELECT current_balance, last_entry_id, last_posted_at, version
                  FROM merchant_ledger_balance_locks
                 WHERE merchant_id = $1::uuid AND currency = $2
                """,
                mid,
                currency,
            )
        if row is None:
            return {
                "merchant_id":     mid,
                "currency":        currency,
                "current_balance": 0.0,
                "last_entry_id":   None,
                "last_posted_at":  None,
                "version":         0,
            }
        return {
            "merchant_id":     mid,
            "currency":        currency,
            "current_balance": _f(row["current_balance"]),
            "last_entry_id":   str(row["last_entry_id"]) if row["last_entry_id"] else None,
            "last_posted_at":  row["last_posted_at"].isoformat() if row["last_posted_at"] else None,
            "version":         int(row["version"]),
        }

    async def list_entries(
        self,
        user: UserContext,
        *,
        transaction_type: Optional[str] = None,
        settlement_id:    Optional[str] = None,
        payment_id:       Optional[str] = None,
        order_id:         Optional[str] = None,
        utr_number:       Optional[str] = None,
        from_date:        Optional[str] = None,   # ISO timestamp
        to_date:          Optional[str] = None,
        currency:         str = "INR",
        limit:            int = 50,
        cursor:           Optional[str] = None,   # opaque: "<created_at>|<id>"
    ) -> dict[str, Any]:
        """Paginated, filterable history. Cursor is keyset (created_at, id) DESC."""
        mid = _merchant_id(user)
        limit = max(1, min(int(limit), 200))

        clauses: list[str] = ["merchant_id = $1::uuid", "currency = $2"]
        params: list[Any] = [mid, (currency or "INR").upper()]

        def _add(clause: str, value: Any) -> None:
            params.append(value)
            clauses.append(clause.replace("?", f"${len(params)}"))

        if transaction_type:
            if transaction_type not in ALLOWED_TXN_TYPES:
                raise ValidationError(f"unknown transaction_type {transaction_type!r}")
            _add("transaction_type = ?::merchant_ledger_txn_type", transaction_type)
        if settlement_id:
            _add("settlement_id = ?::uuid", _parse_uuid(settlement_id, "settlement_id"))
        if payment_id:
            _add("payment_id = ?::uuid", _parse_uuid(payment_id, "payment_id"))
        if order_id:
            _add("order_id = ?::uuid", _parse_uuid(order_id, "order_id"))
        if utr_number:
            _add("utr_number = ?", utr_number)
        if from_date:
            _add("created_at >= ?::timestamptz", from_date)
        if to_date:
            _add("created_at <= ?::timestamptz", to_date)

        # Branch isolation for branch users.
        if user.is_branch_user and user.branch_id:
            _add("branch_id = ?::uuid", str(user.branch_id))

        # Keyset cursor.
        if cursor:
            try:
                cur_ts, cur_id = cursor.split("|", 1)
            except ValueError as e:
                raise ValidationError("invalid cursor") from e
            # Validate cursor pieces before hitting SQL casts.
            try:
                datetime.fromisoformat(cur_ts.replace("Z", "+00:00"))
            except Exception as e:
                raise ValidationError("invalid cursor timestamp") from e
            params.append(cur_ts)
            params.append(_parse_uuid(cur_id, "cursor id"))
            clauses.append(
                f"(created_at, id) < (${len(params) - 1}::timestamptz, ${len(params)}::uuid)"
            )

        params.append(limit + 1)  # +1 to detect next page
        sql = f"""
            SELECT id, merchant_id, branch_id, ledger_reference, transaction_type,
                   debit_amount, credit_amount, balance_after, currency,
                   source_type, source_id, settlement_id, payment_id, order_id,
                   bank_reference, utr_number, idempotency_key, metadata,
                   created_at, created_by
              FROM merchant_ledger
             WHERE {' AND '.join(clauses)}
             ORDER BY created_at DESC, id DESC
             LIMIT ${len(params)}
        """
        async with get_connection() as c:
            rows = await c.fetch(sql, *params)

        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor: Optional[str] = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = f"{last['created_at'].isoformat()}|{last['id']}"

        return {
            "items":       [_row_to_dict(r) for r in rows],
            "next_cursor": next_cursor,
            "has_more":    has_more,
        }

    async def get_entry(self, user: UserContext, entry_id: str) -> dict[str, Any]:
        mid = _merchant_id(user)
        async with get_connection() as c:
            row = await c.fetchrow(
                """
                SELECT id, merchant_id, branch_id, ledger_reference, transaction_type,
                       debit_amount, credit_amount, balance_after, currency,
                       source_type, source_id, settlement_id, payment_id, order_id,
                       bank_reference, utr_number, idempotency_key, metadata,
                       created_at, created_by
                  FROM merchant_ledger
                 WHERE id = $1::uuid AND merchant_id = $2::uuid
                """,
                entry_id,
                mid,
            )
        if row is None:
            raise NotFoundError(f"ledger entry {entry_id} not found")
        return _row_to_dict(row)

    async def verify_consistency(
        self,
        user: UserContext,
        *,
        currency: str = "INR",
    ) -> dict[str, Any]:
        """Recompute balance from movement sum and compare to running balance."""
        mid = _merchant_id(user)
        async with get_connection() as c:
            raw = await c.fetchval(
                "SELECT fn_check_merchant_ledger_consistency($1::uuid, $2)",
                mid,
                (currency or "INR").upper(),
            )
        if isinstance(raw, str):
            raw = json.loads(raw)
        # Cast numerics for JSON cleanliness.
        for k in ("sum_of_movements", "lock_balance", "last_balance_after"):
            if k in raw:
                raw[k] = _f(raw[k])
        return raw


merchant_ledger_service = MerchantLedgerService()
