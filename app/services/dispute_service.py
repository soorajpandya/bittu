"""
Dispute service — Phase 7 (chargebacks, customer complaints, fraud cases).

A dispute models a contested transaction. It has a lifecycle:

    opened → under_review → evidence_submitted → won|lost
                                              ↘  withdrawn

When a dispute resolves to ``lost`` we post a DEBIT to merchant_ledger using
transaction_type='chargeback' so the merchant balance reflects reality.
``won`` and ``withdrawn`` close the case without ledger movement.

Append-only ``dispute_events`` rows record every status change and note for
forensic replay; the table has BEFORE UPDATE/DELETE triggers raising P0002.
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

_KINDS = ("chargeback", "customer_complaint", "fraud", "service_issue", "duplicate", "other")
_ALLOWED_TRANSITIONS = {
    "opened":              {"under_review", "evidence_submitted", "won", "lost", "withdrawn"},
    "under_review":        {"evidence_submitted", "won", "lost", "withdrawn"},
    "evidence_submitted":  {"won", "lost", "withdrawn"},
    "won":        set(),
    "lost":       set(),
    "withdrawn":  set(),
}
_TERMINAL = {"won", "lost", "withdrawn"}


def _row_to_dispute(r) -> dict:
    if r is None:
        return {}
    ev = r["evidence"]
    if isinstance(ev, str):
        ev = json.loads(ev)
    nt = r["notes"]
    if isinstance(nt, str):
        nt = json.loads(nt)
    return {
        "id":                int(r["id"]),
        "dispute_uuid":      str(r["dispute_uuid"]),
        "merchant_id":       str(r["merchant_id"]),
        "payment_id":        str(r["payment_id"]) if r["payment_id"] else None,
        "refund_id":         int(r["refund_id"]) if r["refund_id"] else None,
        "order_id":          str(r["order_id"]) if r["order_id"] else None,
        "kind":              r["kind"],
        "status":            r["status"],
        "amount":            str(r["amount"]),
        "currency":          r["currency"],
        "customer_reference": r["customer_reference"],
        "bank_case_id":      r["bank_case_id"],
        "evidence":          ev or {},
        "notes":             nt or {},
        "opened_by_user_id": str(r["opened_by_user_id"]) if r["opened_by_user_id"] else None,
        "opened_by_admin_id": str(r["opened_by_admin_id"]) if r["opened_by_admin_id"] else None,
        "assigned_admin_id": str(r["assigned_admin_id"]) if r["assigned_admin_id"] else None,
        "outcome":           r["outcome"],
        "resolution_notes":  r["resolution_notes"],
        "ledger_entry_id":   str(r["ledger_entry_id"]) if r["ledger_entry_id"] else None,
        "due_at":            r["due_at"].isoformat() if r["due_at"] else None,
        "opened_at":         r["opened_at"].isoformat(),
        "resolved_at":       r["resolved_at"].isoformat() if r["resolved_at"] else None,
        "created_at":        r["created_at"].isoformat(),
        "updated_at":        r["updated_at"].isoformat(),
    }


def _row_to_event(r) -> dict:
    pl = r["payload"]
    if isinstance(pl, str):
        pl = json.loads(pl)
    return {
        "id":           int(r["id"]),
        "dispute_id":   int(r["dispute_id"]),
        "event_type":   r["event_type"],
        "from_status":  r["from_status"],
        "to_status":    r["to_status"],
        "payload":      pl or {},
        "actor_user_id":  str(r["actor_user_id"]) if r["actor_user_id"] else None,
        "actor_admin_id": str(r["actor_admin_id"]) if r["actor_admin_id"] else None,
        "actor_label":  r["actor_label"],
        "created_at":   r["created_at"].isoformat(),
    }


async def _append_event(
    c,
    *,
    dispute_id: int,
    event_type: str,
    from_status: Optional[str] = None,
    to_status: Optional[str] = None,
    payload: Optional[dict] = None,
    actor_user_id: Optional[str | UUID] = None,
    actor_admin_id: Optional[str | UUID] = None,
    actor_label: Optional[str] = None,
):
    await c.execute(
        """
        INSERT INTO dispute_events
            (dispute_id, event_type, from_status, to_status, payload,
             actor_user_id, actor_admin_id, actor_label)
        VALUES ($1, $2, $3::dispute_status_enum, $4::dispute_status_enum, $5::jsonb,
                $6::uuid, $7::uuid, $8)
        """,
        dispute_id, event_type, from_status, to_status,
        json.dumps(payload or {}),
        str(actor_user_id) if actor_user_id else None,
        str(actor_admin_id) if actor_admin_id else None,
        actor_label,
    )


class DisputeService:
    # ────────────────────────────────────────────────────────────────
    # Open
    # ────────────────────────────────────────────────────────────────
    async def open_dispute(
        self,
        *,
        merchant_id: str | UUID,
        kind: str,
        amount: Decimal | str | float,
        payment_id: Optional[str | UUID] = None,
        order_id: Optional[str | UUID] = None,
        refund_id: Optional[int] = None,
        currency: str = "INR",
        customer_reference: Optional[str] = None,
        bank_case_id: Optional[str] = None,
        evidence: Optional[dict] = None,
        notes: Optional[dict] = None,
        due_at=None,
        opened_by_user_id: Optional[str | UUID] = None,
        opened_by_admin_id: Optional[str | UUID] = None,
        assigned_admin_id: Optional[str | UUID] = None,
    ) -> dict:
        if kind not in _KINDS:
            raise ValidationError(f"kind must be one of {_KINDS}")
        amt = Decimal(str(amount))
        if amt <= 0:
            raise ValidationError("amount must be > 0")

        async with get_transaction() as c:
            row = await c.fetchrow(
                """
                INSERT INTO disputes (
                    merchant_id, payment_id, refund_id, order_id,
                    kind, status, amount, currency,
                    customer_reference, bank_case_id, evidence, notes,
                    opened_by_user_id, opened_by_admin_id, assigned_admin_id,
                    due_at
                )
                VALUES (
                    $1::uuid, $2::uuid, $3, $4::uuid,
                    $5::dispute_kind_enum, 'opened', $6, $7,
                    $8, $9, $10::jsonb, $11::jsonb,
                    $12::uuid, $13::uuid, $14::uuid,
                    $15
                )
                RETURNING *
                """,
                str(merchant_id),
                str(payment_id) if payment_id else None,
                refund_id,
                str(order_id) if order_id else None,
                kind, amt, currency.upper(),
                customer_reference, bank_case_id,
                json.dumps(evidence or {}),
                json.dumps(notes or {}),
                str(opened_by_user_id) if opened_by_user_id else None,
                str(opened_by_admin_id) if opened_by_admin_id else None,
                str(assigned_admin_id) if assigned_admin_id else None,
                due_at,
            )
            await _append_event(
                c,
                dispute_id=int(row["id"]),
                event_type="opened",
                to_status="opened",
                payload={"kind": kind, "amount": str(amt)},
                actor_user_id=opened_by_user_id,
                actor_admin_id=opened_by_admin_id,
            )

        result = _row_to_dispute(row)
        await audit_service.record(
            action="dispute.opened",
            actor_type="admin" if opened_by_admin_id else "user",
            actor_user_id=opened_by_admin_id or opened_by_user_id,
            merchant_id=merchant_id,
            resource_type="dispute",
            resource_id=str(result["id"]),
            payload={"kind": kind, "amount": str(amt)},
        )
        return result

    # ────────────────────────────────────────────────────────────────
    # Transition
    # ────────────────────────────────────────────────────────────────
    async def transition(
        self,
        dispute_id: int,
        *,
        merchant_id: Optional[str | UUID],
        new_status: str,
        outcome: Optional[str] = None,
        resolution_notes: Optional[str] = None,
        evidence_patch: Optional[dict] = None,
        notes_patch: Optional[dict] = None,
        actor_user_id: Optional[str | UUID] = None,
        actor_admin_id: Optional[str | UUID] = None,
        actor_label: Optional[str] = None,
    ) -> dict:
        if new_status not in _ALLOWED_TRANSITIONS:
            raise ValidationError(f"invalid status: {new_status}")

        async with get_transaction() as c:
            row = await c.fetchrow(
                "SELECT * FROM disputes WHERE id = $1 FOR UPDATE", dispute_id
            )
            if row is None:
                raise NotFoundError("dispute not found")
            if merchant_id is not None and str(row["merchant_id"]) != str(merchant_id):
                raise NotFoundError("dispute not found")

            cur = row["status"]
            if new_status not in _ALLOWED_TRANSITIONS[cur]:
                raise ConflictError(f"cannot transition dispute from {cur} to {new_status}")

            ledger_entry_id: Optional[str] = (
                str(row["ledger_entry_id"]) if row["ledger_entry_id"] else None
            )

            if new_status == "lost" and ledger_entry_id is None:
                idem = f"dispute:{row['dispute_uuid']}"
                ledger_row = await c.fetchrow(
                    """
                    SELECT fn_post_merchant_ledger_entry(
                        $1::uuid, NULL, 'chargeback'::merchant_ledger_txn_type,
                        $2, 0, $3, 'dispute', NULL, NULL, $4::uuid, $5::uuid,
                        NULL, NULL, $6::text,
                        jsonb_build_object('dispute_id', $7::bigint, 'kind', $8::text),
                        $9::uuid
                    ) AS entry
                    """,
                    str(row["merchant_id"]),
                    Decimal(str(row["amount"])),
                    row["currency"],
                    str(row["payment_id"]) if row["payment_id"] else None,
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
                UPDATE disputes
                   SET status = $2::dispute_status_enum,
                       outcome = COALESCE($3::dispute_outcome_enum, outcome),
                       resolution_notes = COALESCE($4, resolution_notes),
                       evidence = CASE WHEN $5::jsonb IS NOT NULL
                                       THEN evidence || $5::jsonb ELSE evidence END,
                       notes    = CASE WHEN $6::jsonb IS NOT NULL
                                       THEN notes    || $6::jsonb ELSE notes END,
                       ledger_entry_id = COALESCE($7::uuid, ledger_entry_id),
                       resolved_at = CASE WHEN $2 IN ('won','lost','withdrawn')
                                          THEN now() ELSE resolved_at END
                 WHERE id = $1
                 RETURNING *
                """,
                dispute_id,
                new_status,
                outcome if new_status in _TERMINAL else None,
                resolution_notes,
                json.dumps(evidence_patch) if evidence_patch else None,
                json.dumps(notes_patch) if notes_patch else None,
                ledger_entry_id,
            )
            await _append_event(
                c,
                dispute_id=dispute_id,
                event_type="status_changed" if new_status not in _TERMINAL else "resolved",
                from_status=cur,
                to_status=new_status,
                payload={
                    "outcome": outcome,
                    "resolution_notes": resolution_notes,
                    "ledger_entry_id": ledger_entry_id,
                },
                actor_user_id=actor_user_id,
                actor_admin_id=actor_admin_id,
                actor_label=actor_label,
            )

        result = _row_to_dispute(updated)
        await audit_service.record(
            action=f"dispute.{new_status}",
            actor_type="admin" if actor_admin_id else "user",
            actor_user_id=actor_admin_id or actor_user_id,
            merchant_id=row["merchant_id"],
            resource_type="dispute",
            resource_id=str(dispute_id),
            payload={"from": cur, "to": new_status, "outcome": outcome},
        )
        return result

    # ────────────────────────────────────────────────────────────────
    # Assign / note
    # ────────────────────────────────────────────────────────────────
    async def assign(
        self,
        dispute_id: int,
        *,
        merchant_id: Optional[str | UUID],
        assigned_admin_id: str | UUID,
        actor_admin_id: Optional[str | UUID] = None,
    ) -> dict:
        async with get_transaction() as c:
            row = await c.fetchrow(
                "SELECT merchant_id FROM disputes WHERE id = $1 FOR UPDATE", dispute_id
            )
            if row is None:
                raise NotFoundError("dispute not found")
            if merchant_id is not None and str(row["merchant_id"]) != str(merchant_id):
                raise NotFoundError("dispute not found")
            updated = await c.fetchrow(
                "UPDATE disputes SET assigned_admin_id = $2::uuid WHERE id = $1 RETURNING *",
                dispute_id, str(assigned_admin_id),
            )
            await _append_event(
                c, dispute_id=dispute_id, event_type="assigned",
                payload={"assigned_admin_id": str(assigned_admin_id)},
                actor_admin_id=actor_admin_id,
            )
        return _row_to_dispute(updated)

    async def add_note(
        self,
        dispute_id: int,
        *,
        merchant_id: Optional[str | UUID],
        note: str,
        actor_user_id: Optional[str | UUID] = None,
        actor_admin_id: Optional[str | UUID] = None,
    ) -> dict:
        if not note or not note.strip():
            raise ValidationError("note is required")
        async with get_transaction() as c:
            row = await c.fetchrow(
                "SELECT merchant_id FROM disputes WHERE id = $1", dispute_id
            )
            if row is None:
                raise NotFoundError("dispute not found")
            if merchant_id is not None and str(row["merchant_id"]) != str(merchant_id):
                raise NotFoundError("dispute not found")
            await _append_event(
                c, dispute_id=dispute_id, event_type="note",
                payload={"note": note},
                actor_user_id=actor_user_id,
                actor_admin_id=actor_admin_id,
            )
        return await self.get(dispute_id, merchant_id=merchant_id)

    # ────────────────────────────────────────────────────────────────
    # Read
    # ────────────────────────────────────────────────────────────────
    async def get(self, dispute_id: int, *, merchant_id: Optional[str | UUID] = None) -> dict:
        async with get_connection() as c:
            row = await c.fetchrow("SELECT * FROM disputes WHERE id = $1", dispute_id)
        if row is None:
            raise NotFoundError("dispute not found")
        if merchant_id is not None and str(row["merchant_id"]) != str(merchant_id):
            raise NotFoundError("dispute not found")
        return _row_to_dispute(row)

    async def list_disputes(
        self,
        *,
        merchant_id: Optional[str | UUID] = None,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        payment_id: Optional[str | UUID] = None,
        assigned_admin_id: Optional[str | UUID] = None,
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
            clauses.append(f"status = ${len(params)}::dispute_status_enum")
        if kind is not None:
            params.append(kind)
            clauses.append(f"kind = ${len(params)}::dispute_kind_enum")
        if payment_id is not None:
            params.append(str(payment_id))
            clauses.append(f"payment_id = ${len(params)}::uuid")
        if assigned_admin_id is not None:
            params.append(str(assigned_admin_id))
            clauses.append(f"assigned_admin_id = ${len(params)}::uuid")
        if from_ts is not None:
            params.append(from_ts); clauses.append(f"created_at >= ${len(params)}")
        if to_ts is not None:
            params.append(to_ts); clauses.append(f"created_at <  ${len(params)}")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(min(int(limit), 500))
        params.append(int(offset))
        sql = (
            f"SELECT * FROM disputes {where} "
            f"ORDER BY id DESC LIMIT ${len(params)-1} OFFSET ${len(params)}"
        )
        async with get_connection() as c:
            rows = await c.fetch(sql, *params)
        return [_row_to_dispute(r) for r in rows]

    async def list_events(
        self, dispute_id: int, *, merchant_id: Optional[str | UUID] = None
    ) -> list[dict]:
        # ensure dispute is visible to caller
        await self.get(dispute_id, merchant_id=merchant_id)
        async with get_connection() as c:
            rows = await c.fetch(
                "SELECT * FROM dispute_events WHERE dispute_id = $1 ORDER BY id ASC",
                dispute_id,
            )
        return [_row_to_event(r) for r in rows]

    def to_csv(self, disputes: list[dict]) -> dict:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "id", "dispute_uuid", "merchant_id", "payment_id", "order_id",
            "kind", "status", "outcome", "amount", "currency",
            "bank_case_id", "assigned_admin_id", "ledger_entry_id",
            "opened_at", "resolved_at",
        ])
        for d in disputes:
            w.writerow([
                d["id"], d["dispute_uuid"], d["merchant_id"], d["payment_id"] or "",
                d["order_id"] or "", d["kind"], d["status"], d["outcome"] or "",
                d["amount"], d["currency"], d["bank_case_id"] or "",
                d["assigned_admin_id"] or "", d["ledger_entry_id"] or "",
                d["opened_at"], d["resolved_at"] or "",
            ])
        return {
            "filename": "disputes.csv",
            "content_type": "text/csv",
            "body": buf.getvalue(),
        }


dispute_service = DisputeService()
