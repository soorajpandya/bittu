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

    # ────────────────────────────────────────────────────────────────
    # Razorpay deep wiring (Phase 5)
    # ────────────────────────────────────────────────────────────────
    _RZP_STATUS_MAP = {
        "open":             "opened",
        "under_review":     "under_review",
        "action_required":  "under_review",
        "won":              "won",
        "lost":             "lost",
        "closed":           "withdrawn",
    }
    _RZP_PHASE_TO_KIND = {
        "chargeback":       "chargeback",
        "fraud":            "fraud",
        "retrieval":        "customer_complaint",
        "pre_arbitration":  "chargeback",
        "arbitration":      "chargeback",
    }

    async def _find_local_by_razorpay_id(
        self, *, merchant_id: str | UUID, razorpay_dispute_id: str
    ) -> Optional[dict]:
        """Local row lookup via the rzp_disputes link table."""
        async with get_connection() as c:
            row = await c.fetchrow(
                """
                SELECT d.*
                FROM rzp_disputes r
                JOIN disputes d ON d.dispute_uuid = r.internal_dispute_id
                WHERE r.dispute_id  = $1
                  AND d.merchant_id = $2::uuid
                LIMIT 1
                """,
                razorpay_dispute_id, str(merchant_id),
            )
        return _row_to_dispute(row) if row else None

    async def _link_rzp_dispute(
        self, *, razorpay_dispute_id: str, internal_dispute_uuid: str
    ) -> None:
        """Stamp `rzp_disputes.internal_dispute_id` so future webhooks find us fast."""
        from app.core.database import get_service_connection
        async with get_service_connection() as conn:
            await conn.execute(
                "UPDATE rzp_disputes SET internal_dispute_id = $2::uuid, updated_at = NOW() "
                "WHERE dispute_id = $1",
                razorpay_dispute_id, internal_dispute_uuid,
            )

    async def upsert_from_razorpay(
        self,
        *,
        rzp_entity: dict,
        merchant_id: str | UUID,
        razorpay_status_override: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Reconcile a Razorpay dispute entity into the local `disputes` table.

        - Locates an existing local row via rzp_disputes.internal_dispute_id.
        - If absent, OPENS a new local row scoped to the matching internal payment
          (resolved via rzp_payments_index) and links it back into rzp_disputes.
        - Idempotent: never duplicates rows; transitions the local row only if
          the gateway state is strictly "ahead" of the local FSM.

        Returns the (possibly updated) local dispute dict, or None if the
        merchant_id is the orphan placeholder (webhook arrived before checkout).
        """
        if str(merchant_id) == "00000000-0000-0000-0000-000000000000":
            return None
        rzp_dispute_id = rzp_entity.get("id")
        if not rzp_dispute_id:
            return None

        # 1. Map gateway → local FSM/kind.
        rzp_status = (razorpay_status_override or rzp_entity.get("status") or "open").lower()
        local_target = self._RZP_STATUS_MAP.get(rzp_status, "opened")
        phase = (rzp_entity.get("phase") or "chargeback").lower()
        kind = self._RZP_PHASE_TO_KIND.get(phase, "chargeback")
        amount = Decimal(str(rzp_entity.get("amount") or 0)) / Decimal(100)
        currency = (rzp_entity.get("currency") or "INR").upper()
        deadline = rzp_entity.get("respond_by") or rzp_entity.get("deadline_at")
        from datetime import datetime, timezone
        due_at = None
        if isinstance(deadline, (int, float)) and deadline > 0:
            due_at = datetime.fromtimestamp(int(deadline), tz=timezone.utc)

        # 2. Resolve internal payment_id from razorpay_payment_id (best-effort).
        internal_payment_uuid: Optional[str] = None
        rzp_payment_id = rzp_entity.get("payment_id")
        if rzp_payment_id:
            async with get_connection() as c:
                row = await c.fetchrow(
                    "SELECT id::text AS id FROM payments "
                    "WHERE razorpay_payment_id = $1 AND restaurant_id = $2::uuid LIMIT 1",
                    rzp_payment_id, str(merchant_id),
                )
            if row:
                internal_payment_uuid = row["id"]

        # 3. Find or create the local dispute row.
        existing = await self._find_local_by_razorpay_id(
            merchant_id=merchant_id, razorpay_dispute_id=rzp_dispute_id,
        )
        if existing is None:
            opened = await self.open_dispute(
                merchant_id=merchant_id,
                kind=kind,
                amount=amount if amount > 0 else Decimal("0.01"),
                payment_id=internal_payment_uuid,
                currency=currency,
                customer_reference=rzp_dispute_id,
                bank_case_id=rzp_entity.get("gateway_dispute_id"),
                evidence={"razorpay": rzp_entity.get("evidence") or {}},
                notes={
                    "razorpay_dispute_id": rzp_dispute_id,
                    "razorpay_phase": phase,
                    "razorpay_reason_code": rzp_entity.get("reason_code"),
                    "razorpay_reason_description": rzp_entity.get("reason_description"),
                },
                due_at=due_at,
                opened_by_admin_id=None,
            )
            await self._link_rzp_dispute(
                razorpay_dispute_id=rzp_dispute_id,
                internal_dispute_uuid=opened["dispute_uuid"],
            )
            existing = opened

        # 4. Transition iff target is strictly newer than current.
        cur = existing["status"]
        if local_target == cur or cur in _TERMINAL:
            return existing
        if local_target not in _ALLOWED_TRANSITIONS.get(cur, set()):
            # Either already ahead or invalid transition — leave as-is.
            return existing

        outcome = None
        if local_target in ("lost", "won", "withdrawn"):
            outcome = local_target

        return await self.transition(
            int(existing["id"]),
            merchant_id=merchant_id,
            new_status=local_target,
            outcome=outcome,
            actor_label="razorpay_webhook",
        )

    async def accept_via_gateway(
        self,
        dispute_id: int,
        *,
        merchant_id: str | UUID,
        actor_user_id: Optional[str | UUID] = None,
        actor_admin_id: Optional[str | UUID] = None,
    ) -> dict:
        """
        Accept a Razorpay dispute (admit liability). Idempotent on
        ``rzp_dispute_accept:{dispute_uuid}`` — replays return the same
        gateway response.
        """
        local = await self.get(dispute_id, merchant_id=merchant_id)
        rzp_dispute_id = (local.get("notes") or {}).get("razorpay_dispute_id")
        if not rzp_dispute_id:
            raise ValidationError("dispute is not linked to a razorpay dispute")
        if local["status"] in _TERMINAL:
            return local

        from app.services.razorpay import disputes as rzp_disputes_api
        try:
            rzp_resp = await rzp_disputes_api.accept_dispute(
                rzp_dispute_id,
                idempotency_key=f"rzp_dispute_accept:{local['dispute_uuid']}",
                merchant_id=str(merchant_id),
            )
        except Exception as exc:
            logger.exception("rzp_dispute_accept_failed",
                             dispute_id=dispute_id, rzp_dispute_id=rzp_dispute_id)
            raise ConflictError(f"razorpay accept failed: {exc!s}")

        # Razorpay returns the (now-lost) dispute entity. Drive the local FSM
        # to 'lost' so the chargeback DEBIT fires via existing transition path.
        return await self.transition(
            dispute_id,
            merchant_id=merchant_id,
            new_status="lost",
            outcome="lost",
            resolution_notes="accepted via razorpay",
            evidence_patch={"razorpay_accept_response": rzp_resp},
            actor_user_id=actor_user_id,
            actor_admin_id=actor_admin_id,
            actor_label="razorpay_accept",
        )

    async def contest_via_gateway(
        self,
        dispute_id: int,
        *,
        merchant_id: str | UUID,
        evidence: dict,
        action: str = "draft",  # draft|submit
        actor_user_id: Optional[str | UUID] = None,
        actor_admin_id: Optional[str | UUID] = None,
    ) -> dict:
        """
        Contest a Razorpay dispute. ``action='draft'`` saves evidence;
        ``action='submit'`` finalises and sends to the network.
        """
        if action not in ("draft", "submit"):
            raise ValidationError("action must be draft|submit")
        if not evidence:
            raise ValidationError("evidence is required")

        local = await self.get(dispute_id, merchant_id=merchant_id)
        rzp_dispute_id = (local.get("notes") or {}).get("razorpay_dispute_id")
        if not rzp_dispute_id:
            raise ValidationError("dispute is not linked to a razorpay dispute")
        if local["status"] in _TERMINAL:
            raise ConflictError(f"dispute is already {local['status']}")

        from app.services.razorpay import disputes as rzp_disputes_api
        try:
            rzp_resp = await rzp_disputes_api.contest_dispute(
                rzp_dispute_id,
                evidence=evidence,
                action=action,
                idempotency_key=f"rzp_dispute_contest:{local['dispute_uuid']}:{action}",
                merchant_id=str(merchant_id),
            )
        except Exception as exc:
            logger.exception("rzp_dispute_contest_failed",
                             dispute_id=dispute_id, rzp_dispute_id=rzp_dispute_id)
            raise ConflictError(f"razorpay contest failed: {exc!s}")

        # On submit, advance local FSM to 'evidence_submitted'.
        if action == "submit" and local["status"] in ("opened", "under_review"):
            return await self.transition(
                dispute_id,
                merchant_id=merchant_id,
                new_status="evidence_submitted",
                evidence_patch={"razorpay_contest_response": rzp_resp,
                                "submitted_evidence": evidence},
                actor_user_id=actor_user_id,
                actor_admin_id=actor_admin_id,
                actor_label="razorpay_contest_submit",
            )

        # Draft: just record the evidence + return the row.
        async with get_transaction() as c:
            await _append_event(
                c,
                dispute_id=dispute_id,
                event_type="evidence_added",
                payload={"action": action, "razorpay_response": rzp_resp,
                         "evidence_keys": sorted(evidence.keys())},
                actor_user_id=actor_user_id,
                actor_admin_id=actor_admin_id,
                actor_label="razorpay_contest_draft",
            )
            await c.execute(
                "UPDATE disputes SET evidence = evidence || $2::jsonb WHERE id = $1",
                dispute_id, json.dumps({"razorpay_contest_draft": rzp_resp,
                                        "draft_evidence": evidence}),
            )
        return await self.get(dispute_id, merchant_id=merchant_id)

    async def sync_from_gateway(
        self,
        dispute_id: int,
        *,
        merchant_id: str | UUID,
    ) -> dict:
        """Re-fetch a dispute from Razorpay and re-run upsert."""
        local = await self.get(dispute_id, merchant_id=merchant_id)
        rzp_dispute_id = (local.get("notes") or {}).get("razorpay_dispute_id")
        if not rzp_dispute_id:
            raise ValidationError("dispute is not linked to a razorpay dispute")

        from app.services.razorpay import disputes as rzp_disputes_api
        rzp_entity = await rzp_disputes_api.fetch_dispute(
            rzp_dispute_id, merchant_id=str(merchant_id),
        )
        synced = await self.upsert_from_razorpay(
            rzp_entity=rzp_entity, merchant_id=merchant_id,
        )
        return synced or local

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
