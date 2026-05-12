"""
Refund service — Phase 7.

Models the refund lifecycle (initiated → processing → succeeded|failed|cancelled).
On transition to ``succeeded`` we post a DEBIT to the merchant_ledger via
``fn_post_merchant_ledger_entry`` with transaction_type='refund' so balances
stay correct.

This service does NOT call any payment gateway. ``gateway_refund_id`` is a
free-form field that operations can fill in if/when a gateway integration is
added later.
"""
from __future__ import annotations

import csv
import io
import json
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from app.core.database import get_connection, get_transaction
from app.core.exceptions import NotFoundError, ValidationError, ConflictError
from app.core.logging import get_logger
from app.services.audit_service import audit_service

logger = get_logger(__name__)

_ALLOWED_TRANSITIONS = {
    "initiated":  {"processing", "succeeded", "failed", "cancelled"},
    "processing": {"succeeded", "failed", "cancelled"},
    "succeeded":  set(),
    "failed":     set(),
    "cancelled":  set(),
}


def _row_to_refund(r) -> dict:
    if r is None:
        return {}
    notes = r["notes"]
    if isinstance(notes, str):
        notes = json.loads(notes)
    return {
        "id":                int(r["id"]),
        "refund_uuid":       str(r["refund_uuid"]),
        "merchant_id":       str(r["merchant_id"]),
        "payment_id":        str(r["payment_id"]),
        "order_id":          str(r["order_id"]) if r["order_id"] else None,
        "amount":            str(r["amount"]),
        "currency":          r["currency"],
        "kind":              r["kind"],
        "status":            r["status"],
        "reason":            r["reason"],
        "customer_contact":  r["customer_contact"],
        "gateway_refund_id": r["gateway_refund_id"],
        "initiated_by_user_id":  str(r["initiated_by_user_id"]) if r["initiated_by_user_id"] else None,
        "initiated_by_admin_id": str(r["initiated_by_admin_id"]) if r["initiated_by_admin_id"] else None,
        "ledger_entry_id":   str(r["ledger_entry_id"]) if r["ledger_entry_id"] else None,
        "notes":             notes or {},
        "failure_reason":    r["failure_reason"],
        "processed_at":      r["processed_at"].isoformat() if r["processed_at"] else None,
        "created_at":        r["created_at"].isoformat(),
        "updated_at":        r["updated_at"].isoformat(),
    }


class RefundService:
    # ────────────────────────────────────────────────────────────────
    # Create
    # ────────────────────────────────────────────────────────────────
    async def create(
        self,
        *,
        merchant_id: str | UUID,
        payment_id: str | UUID,
        amount: Decimal | str | float,
        reason: Optional[str] = None,
        kind: str = "partial",
        order_id: Optional[str | UUID] = None,
        customer_contact: Optional[str] = None,
        notes: Optional[dict] = None,
        initiated_by_user_id: Optional[str | UUID] = None,
        initiated_by_admin_id: Optional[str | UUID] = None,
    ) -> dict:
        amt = Decimal(str(amount))
        if amt <= 0:
            raise ValidationError("amount must be > 0")
        if kind not in ("full", "partial", "goodwill"):
            raise ValidationError("kind must be full|partial|goodwill")

        async with get_transaction() as c:
            # validate payment ownership and refundable balance
            pay = await c.fetchrow(
                "SELECT id, restaurant_id, order_id, amount, status, currency "
                "FROM payments WHERE id = $1::uuid",
                str(payment_id),
            )
            if pay is None:
                raise NotFoundError("payment not found")
            if pay["restaurant_id"] is not None and str(pay["restaurant_id"]) != str(merchant_id):
                raise NotFoundError("payment not found")
            if pay["status"] not in ("paid", "captured", "succeeded", "completed"):
                # be permissive — many callers may not have gateway statuses
                logger.info("refund.payment_status_not_paid", payment_id=str(payment_id), status=pay["status"])

            refundable = await c.fetchval(
                "SELECT fn_refundable_amount($1::uuid, $2::uuid)",
                str(merchant_id), str(payment_id),
            )
            refundable = Decimal(str(refundable or 0))
            if amt > refundable:
                raise ConflictError(
                    f"refund amount {amt} exceeds refundable balance {refundable}"
                )

            row = await c.fetchrow(
                """
                INSERT INTO refunds (
                    merchant_id, payment_id, order_id, amount, currency,
                    kind, status, reason, customer_contact, notes,
                    initiated_by_user_id, initiated_by_admin_id
                )
                VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4, $5,
                    $6::refund_kind_enum, 'initiated', $7, $8, $9::jsonb,
                    $10::uuid, $11::uuid
                )
                RETURNING *
                """,
                str(merchant_id),
                str(payment_id),
                str(order_id) if order_id else (str(pay["order_id"]) if pay["order_id"] else None),
                amt,
                (pay["currency"] or "INR").upper(),
                kind,
                reason,
                customer_contact,
                json.dumps(notes or {}),
                str(initiated_by_user_id) if initiated_by_user_id else None,
                str(initiated_by_admin_id) if initiated_by_admin_id else None,
            )

        result = _row_to_refund(row)
        await audit_service.record(
            action="refund.created",
            actor_type="admin" if initiated_by_admin_id else "user",
            actor_user_id=initiated_by_admin_id or initiated_by_user_id,
            merchant_id=merchant_id,
            resource_type="refund",
            resource_id=str(result["id"]),
            payload={"amount": str(amt), "payment_id": str(payment_id), "kind": kind},
        )
        return result

    # ────────────────────────────────────────────────────────────────
    # Transition
    # ────────────────────────────────────────────────────────────────
    async def transition(
        self,
        refund_id: int,
        *,
        merchant_id: Optional[str | UUID],
        new_status: str,
        gateway_refund_id: Optional[str] = None,
        failure_reason: Optional[str] = None,
        notes: Optional[dict] = None,
        actor_user_id: Optional[str | UUID] = None,
        actor_admin_id: Optional[str | UUID] = None,
    ) -> dict:
        if new_status not in _ALLOWED_TRANSITIONS:
            raise ValidationError(f"invalid status: {new_status}")

        async with get_transaction() as c:
            row = await c.fetchrow(
                "SELECT * FROM refunds WHERE id = $1 FOR UPDATE", refund_id
            )
            if row is None:
                raise NotFoundError("refund not found")
            if merchant_id is not None and str(row["merchant_id"]) != str(merchant_id):
                raise NotFoundError("refund not found")

            cur = row["status"]
            if new_status not in _ALLOWED_TRANSITIONS[cur]:
                raise ConflictError(f"cannot transition refund from {cur} to {new_status}")

            ledger_entry_id: Optional[str] = (
                str(row["ledger_entry_id"]) if row["ledger_entry_id"] else None
            )

            if new_status == "succeeded" and ledger_entry_id is None:
                # Post DEBIT to merchant_ledger so balance reflects the refund.
                idem = f"refund:{row['refund_uuid']}"
                ledger_row = await c.fetchrow(
                    """
                    SELECT fn_post_merchant_ledger_entry(
                        $1::uuid, NULL, 'refund'::merchant_ledger_txn_type,
                        $2, 0, $3, 'refund', NULL, NULL, $4::uuid, $5::uuid,
                        NULL, NULL, $6::text,
                        jsonb_build_object('refund_id', $7::bigint, 'kind', $8::text),
                        $9::uuid
                    ) AS entry
                    """,
                    str(row["merchant_id"]),
                    Decimal(str(row["amount"])),
                    row["currency"],
                    str(row["payment_id"]),
                    str(row["order_id"]) if row["order_id"] else None,
                    idem,
                    int(row["id"]),
                    row["kind"],
                    str(actor_admin_id or actor_user_id) if (actor_admin_id or actor_user_id) else None,
                )
                entry_json = ledger_row["entry"]
                if isinstance(entry_json, str):
                    entry_json = json.loads(entry_json)
                ledger_entry_id = entry_json.get("id") if entry_json else None

            updated = await c.fetchrow(
                """
                UPDATE refunds
                   SET status = $2::refund_status_enum,
                       gateway_refund_id = COALESCE($3, gateway_refund_id),
                       failure_reason    = COALESCE($4, failure_reason),
                       notes             = CASE WHEN $5::jsonb IS NOT NULL
                                                THEN notes || $5::jsonb
                                                ELSE notes END,
                       ledger_entry_id   = COALESCE($6::uuid, ledger_entry_id),
                       processed_at      = CASE WHEN $2 IN ('succeeded','failed','cancelled')
                                                THEN now() ELSE processed_at END
                 WHERE id = $1
                 RETURNING *
                """,
                refund_id,
                new_status,
                gateway_refund_id,
                failure_reason,
                json.dumps(notes) if notes else None,
                ledger_entry_id,
            )

        result = _row_to_refund(updated)
        await audit_service.record(
            action=f"refund.{new_status}",
            actor_type="admin" if actor_admin_id else "user",
            actor_user_id=actor_admin_id or actor_user_id,
            merchant_id=row["merchant_id"],
            resource_type="refund",
            resource_id=str(refund_id),
            payload={
                "from": cur,
                "to": new_status,
                "gateway_refund_id": gateway_refund_id,
                "failure_reason": failure_reason,
            },
        )
        return result

    # ────────────────────────────────────────────────────────────────
    # Read
    # ────────────────────────────────────────────────────────────────
    async def get(self, refund_id: int, *, merchant_id: Optional[str | UUID] = None) -> dict:
        async with get_connection() as c:
            row = await c.fetchrow("SELECT * FROM refunds WHERE id = $1", refund_id)
        if row is None:
            raise NotFoundError("refund not found")
        if merchant_id is not None and str(row["merchant_id"]) != str(merchant_id):
            raise NotFoundError("refund not found")
        return _row_to_refund(row)

    async def list_refunds(
        self,
        *,
        merchant_id: Optional[str | UUID] = None,
        status: Optional[str] = None,
        payment_id: Optional[str | UUID] = None,
        order_id: Optional[str | UUID] = None,
        kind: Optional[str] = None,
        from_ts=None,
        to_ts=None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        if status is not None:
            params.append(status)
            clauses.append(f"status = ${len(params)}::refund_status_enum")
        if payment_id is not None:
            params.append(str(payment_id))
            clauses.append(f"payment_id = ${len(params)}::uuid")
        if order_id is not None:
            params.append(str(order_id))
            clauses.append(f"order_id = ${len(params)}::uuid")
        if kind is not None:
            params.append(kind)
            clauses.append(f"kind = ${len(params)}::refund_kind_enum")
        if from_ts is not None:
            params.append(from_ts); clauses.append(f"created_at >= ${len(params)}")
        if to_ts is not None:
            params.append(to_ts); clauses.append(f"created_at <  ${len(params)}")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(min(int(limit), 500))
        params.append(int(offset))
        sql = (
            f"SELECT * FROM refunds {where} "
            f"ORDER BY id DESC LIMIT ${len(params)-1} OFFSET ${len(params)}"
        )
        async with get_connection() as c:
            rows = await c.fetch(sql, *params)
        return [_row_to_refund(r) for r in rows]

    async def refundable_amount(
        self, *, merchant_id: str | UUID, payment_id: str | UUID
    ) -> Decimal:
        async with get_connection() as c:
            v = await c.fetchval(
                "SELECT fn_refundable_amount($1::uuid, $2::uuid)",
                str(merchant_id), str(payment_id),
            )
        return Decimal(str(v or 0))

    # ────────────────────────────────────────────────────────────────
    # CSV
    # ────────────────────────────────────────────────────────────────
    def to_csv(self, refunds: list[dict]) -> dict:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "id", "refund_uuid", "merchant_id", "payment_id", "order_id",
            "amount", "currency", "kind", "status", "reason",
            "gateway_refund_id", "ledger_entry_id", "processed_at", "created_at",
        ])
        for r in refunds:
            w.writerow([
                r["id"], r["refund_uuid"], r["merchant_id"], r["payment_id"],
                r["order_id"] or "", r["amount"], r["currency"], r["kind"],
                r["status"], r["reason"] or "", r["gateway_refund_id"] or "",
                r["ledger_entry_id"] or "", r["processed_at"] or "", r["created_at"],
            ])
        return {
            "filename": "refunds.csv",
            "content_type": "text/csv",
            "body": buf.getvalue(),
        }


refund_service = RefundService()
