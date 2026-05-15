"""
Refund service — Phase 7 + Phase 4 (Razorpay deep wiring).

Models the refund lifecycle (initiated → processing → succeeded|failed|cancelled).
On transition to ``succeeded`` we post a DEBIT to the merchant_ledger via
``fn_post_merchant_ledger_entry`` with transaction_type='refund' so balances
stay correct.

`create()` is the pure local-only path retained for offline / cash refunds.
`create_and_dispatch()` (Phase 4) is the gateway-aware orchestrator: it
creates the local row, calls Razorpay's refund API (idempotent on
``refund:{refund_uuid}``), mirrors into ``rzp_refunds``, and transitions the
local row according to the gateway's reply (pending → processing,
processed → succeeded → ledger DEBIT, failed → failed).
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
    # Gateway-aware orchestrator (Phase 4)
    # ────────────────────────────────────────────────────────────────
    async def create_and_dispatch(
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
        speed: str = "normal",  # razorpay: normal|optimum
        initiated_by_user_id: Optional[str | UUID] = None,
        initiated_by_admin_id: Optional[str | UUID] = None,
    ) -> dict:
        """
        Create a refund and (if the payment was online) dispatch it to Razorpay.

        Idempotent on ``refund:{refund_uuid}`` — the gateway's idempotency
        cache returns the same razorpay refund row on retry. The local row's
        terminal state is governed by the gateway response, NOT the API call:

          * razorpay status='processed' → local 'succeeded' → ledger DEBIT
          * razorpay status='pending'   → local 'processing' (webhook later
                                          fires the success transition)
          * razorpay status='failed'    → local 'failed'

        For non-online payments (cash / upi-cash-equivalent), this falls
        through to plain ``create()`` — the operator owns the ledger
        movement via the existing transition() workflow.
        """
        # 1. Pre-fetch the gateway payment id BEFORE touching the local row,
        #    so we know whether to dispatch.
        async with get_connection() as c:
            pay = await c.fetchrow(
                """
                SELECT id::text                AS id,
                       razorpay_payment_id     AS rzp_payment_id,
                       method                  AS method,
                       amount                  AS amount,
                       status                  AS status
                FROM payments
                WHERE id = $1::uuid
                """,
                str(payment_id),
            )
        if pay is None:
            raise NotFoundError("payment not found")
        rzp_payment_id = pay["rzp_payment_id"]

        # 2. Create the local row (existing path — full validation, audit).
        local = await self.create(
            merchant_id=merchant_id,
            payment_id=payment_id,
            amount=amount,
            reason=reason,
            kind=kind,
            order_id=order_id,
            customer_contact=customer_contact,
            notes=notes,
            initiated_by_user_id=initiated_by_user_id,
            initiated_by_admin_id=initiated_by_admin_id,
        )

        # 3. If this isn't an online payment, we're done.
        if not rzp_payment_id:
            return local

        # 4. Dispatch to Razorpay (outside any DB txn — gateway latency).
        from app.services.razorpay import refunds as rzp_refunds_api

        amt = Decimal(str(amount))
        amount_paise = int((amt * 100).to_integral_value())
        idem_key = f"refund:{local['refund_uuid']}"

        try:
            rzp_resp = await rzp_refunds_api.create_refund(
                payment_id=rzp_payment_id,
                amount_paise=amount_paise,
                speed=speed,
                notes={
                    "internal_refund_id": str(local["id"]),
                    "internal_refund_uuid": str(local["refund_uuid"]),
                    "reason": reason or "",
                },
                idempotency_key=idem_key,
                merchant_id=str(merchant_id),
            )
        except Exception as exc:
            logger.exception("refund.gateway_call_failed",
                             refund_id=local["id"], payment_id=str(payment_id))
            failed = await self.transition(
                int(local["id"]),
                merchant_id=merchant_id,
                new_status="failed",
                failure_reason=f"gateway_error: {str(exc)[:200]}",
                actor_user_id=initiated_by_user_id,
                actor_admin_id=initiated_by_admin_id,
            )
            return failed

        # 5. Mirror the gateway response into rzp_refunds so the eventual
        #    webhook UPSERT is a true no-op.
        await self._mirror_rzp_refund(
            rzp_resp=rzp_resp,
            internal_refund_id=local["id"],
            internal_refund_uuid=local["refund_uuid"],
            merchant_id=str(merchant_id),
            initiated_by=initiated_by_admin_id or initiated_by_user_id,
        )

        # 6. Transition the local row based on gateway state.
        gw_status = (rzp_resp.get("status") or "pending").lower()
        gw_refund_id = rzp_resp.get("id")
        if gw_status == "processed":
            new_status = "succeeded"
            failure_reason = None
        elif gw_status == "failed":
            new_status = "failed"
            failure_reason = (
                rzp_resp.get("notes", {}).get("failure_reason")
                if isinstance(rzp_resp.get("notes"), dict) else None
            ) or "razorpay_refund_failed"
        else:  # pending and anything we don't recognise
            new_status = "processing"
            failure_reason = None

        return await self.transition(
            int(local["id"]),
            merchant_id=merchant_id,
            new_status=new_status,
            gateway_refund_id=gw_refund_id,
            failure_reason=failure_reason,
            notes={"razorpay_status": gw_status, "speed_processed": rzp_resp.get("speed_processed")},
            actor_user_id=initiated_by_user_id,
            actor_admin_id=initiated_by_admin_id,
        )

    async def _mirror_rzp_refund(
        self,
        *,
        rzp_resp: dict,
        internal_refund_id: int,
        internal_refund_uuid: str,
        merchant_id: str,
        initiated_by: Optional[str | UUID] = None,
    ) -> None:
        """UPSERT into rzp_refunds so webhook idempotency holds."""
        from app.core.database import get_service_connection

        refund_id = rzp_resp.get("id")
        if not refund_id:
            return
        try:
            async with get_service_connection() as c:
                await c.execute(
                    """
                    INSERT INTO rzp_refunds (
                        refund_id, razorpay_payment_id, internal_refund_id,
                        merchant_id, amount_paise, currency,
                        speed_requested, speed_processed,
                        status, reason, initiated_by, batch_id, acquirer_data,
                        notes, raw_payload, processed_at
                    ) VALUES (
                        $1, $2, $3::uuid,
                        $4::uuid, $5, $6,
                        $7, $8,
                        $9::rzp_refund_state, $10, $11::uuid, $12, $13::jsonb,
                        $14::jsonb, $15::jsonb,
                        CASE WHEN $16::bigint IS NULL THEN NULL
                             ELSE to_timestamp($16::bigint) END
                    )
                    ON CONFLICT (refund_id) DO UPDATE SET
                        status              = EXCLUDED.status,
                        speed_processed     = COALESCE(EXCLUDED.speed_processed, rzp_refunds.speed_processed),
                        internal_refund_id  = COALESCE(rzp_refunds.internal_refund_id, EXCLUDED.internal_refund_id),
                        acquirer_data       = COALESCE(EXCLUDED.acquirer_data, rzp_refunds.acquirer_data),
                        processed_at        = COALESCE(EXCLUDED.processed_at, rzp_refunds.processed_at),
                        raw_payload         = EXCLUDED.raw_payload,
                        updated_at          = NOW()
                    """,
                    refund_id,
                    rzp_resp.get("payment_id"),
                    internal_refund_uuid,
                    merchant_id,
                    int(rzp_resp.get("amount") or 0),
                    rzp_resp.get("currency") or "INR",
                    rzp_resp.get("speed_requested"),
                    rzp_resp.get("speed_processed"),
                    (rzp_resp.get("status") or "pending").lower(),
                    rzp_resp.get("notes", {}).get("reason") if isinstance(rzp_resp.get("notes"), dict) else None,
                    str(initiated_by) if initiated_by else None,
                    rzp_resp.get("batch_id"),
                    json.dumps(rzp_resp.get("acquirer_data") or {}) if rzp_resp.get("acquirer_data") else None,
                    json.dumps(rzp_resp.get("notes") or {}),
                    json.dumps(rzp_resp),
                    rzp_resp.get("processed_at") or rzp_resp.get("created_at"),
                )
        except Exception:  # noqa: BLE001
            logger.exception("rzp_refund_mirror_failed", refund_id=refund_id)

    # ────────────────────────────────────────────────────────────────
    # Webhook hook — called by webhook_dispatcher on refund.processed
    # ────────────────────────────────────────────────────────────────
    async def transition_by_gateway_id(
        self,
        *,
        merchant_id: str,
        gateway_refund_id: str,
        new_status: str,
        failure_reason: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Find the local refund row whose gateway_refund_id matches and
        transition it. Used by the Razorpay webhook to fire the ledger DEBIT
        when the refund truly settles. Returns None if no local row exists
        (refund was issued out-of-band on the Razorpay dashboard).
        """
        async with get_connection() as c:
            row = await c.fetchrow(
                "SELECT id, status FROM refunds "
                "WHERE merchant_id = $1::uuid AND gateway_refund_id = $2 LIMIT 1",
                str(merchant_id), gateway_refund_id,
            )
        if row is None:
            return None
        if row["status"] not in _ALLOWED_TRANSITIONS:
            return None
        if new_status not in _ALLOWED_TRANSITIONS.get(row["status"], set()):
            # Already terminal — nothing to do (idempotent).
            return None
        return await self.transition(
            int(row["id"]),
            merchant_id=merchant_id,
            new_status=new_status,
            failure_reason=failure_reason,
        )

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
