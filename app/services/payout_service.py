"""
Payout / Disbursement service — Phase 4 of Bittu fintech reconciliation core.

Owns the lifecycle of merchant payouts:

    requested  ─approve→ approved ─batch→ queued ─generate→ processing
                                                          ─send→ sent
                                                          ─complete→ completed
                                                          ─fail→ failed (reverses ledger)

State machine
─────────────
    requested ─→ approved | rejected | cancelled
    approved  ─→ queued (via batch) | rejected
    queued    ─→ processing (file generated)
    processing ─→ sent (operator marked sent + bank acked)
    sent      ─→ completed | failed
    failed    ─→ (terminal; reversal posted)

HARD RULE: NO gateway calls. The service produces a CSV the operator manually
uploads to the bank portal; the operator then marks the payouts sent/completed
based on bank acks.

merchant_ledger debit happens ONLY on `mark_sent`. Reversal credit happens
ONLY on `mark_failed` AFTER the request was already 'sent'.

Admin/merchant separation
─────────────────────────
Reads accept ``merchant_id`` (None for admin global). Writes scoped:
  • merchant: create_beneficiary, request_payout, cancel_payout (own only)
  • admin:    approve, reject, batch ops, mark_sent / completed / failed
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Optional
from uuid import UUID

from app.core.database import get_connection, get_transaction
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)


# Status sets used in the state machine
PENDING_STATUSES   = frozenset({"requested", "approved", "queued", "processing"})
TERMINAL_STATUSES  = frozenset({"completed", "rejected", "cancelled", "failed"})
LIVE_LEDGER_STATUS = "sent"   # the only status with a posted ledger debit


def _f(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(round(v, 4))
    return float(v)


def _row_to_payout(r) -> dict:
    if r is None:
        return {}
    md = r.get("metadata") if isinstance(r, dict) else r["metadata"]
    if isinstance(md, str):
        md = json.loads(md)
    g = (lambda k: r[k]) if not isinstance(r, dict) else r.get
    out = {
        "id":               str(g("id")),
        "payout_reference": g("payout_reference"),
        "merchant_id":      str(g("merchant_id")),
        "branch_id":        str(g("branch_id")) if g("branch_id") else None,
        "beneficiary_id":   str(g("beneficiary_id")),
        "amount":           _f(g("amount")),
        "currency":         g("currency"),
        "method":           g("method"),
        "status":           g("status"),
        "requested_by":     str(g("requested_by")),
        "requested_at":     g("requested_at").isoformat(),
        "approved_by":      str(g("approved_by")) if g("approved_by") else None,
        "approved_at":      g("approved_at").isoformat() if g("approved_at") else None,
        "rejected_by":      str(g("rejected_by")) if g("rejected_by") else None,
        "rejected_at":      g("rejected_at").isoformat() if g("rejected_at") else None,
        "rejection_reason": g("rejection_reason"),
        "cancelled_at":     g("cancelled_at").isoformat() if g("cancelled_at") else None,
        "batch_id":         str(g("batch_id")) if g("batch_id") else None,
        "ledger_entry_id":  str(g("ledger_entry_id")) if g("ledger_entry_id") else None,
        "reversal_entry_id": str(g("reversal_entry_id")) if g("reversal_entry_id") else None,
        "utr_number":       g("utr_number"),
        "bank_reference":   g("bank_reference"),
        "sent_at":          g("sent_at").isoformat() if g("sent_at") else None,
        "completed_at":     g("completed_at").isoformat() if g("completed_at") else None,
        "failed_at":        g("failed_at").isoformat() if g("failed_at") else None,
        "failure_reason":   g("failure_reason"),
        "notes":            g("notes"),
        "idempotency_key":  g("idempotency_key"),
        "metadata":         md or {},
        "created_at":       g("created_at").isoformat(),
        "updated_at":       g("updated_at").isoformat(),
    }
    return out


def _row_to_beneficiary(r) -> dict:
    if r is None:
        return {}
    md = r["metadata"]
    if isinstance(md, str):
        md = json.loads(md)
    return {
        "id":                   str(r["id"]),
        "merchant_id":          str(r["merchant_id"]),
        "label":                r["label"],
        "type":                 r["type"],
        "account_holder":       r["account_holder"],
        "account_number_last4": r["account_number_last4"],
        "ifsc":                 r["ifsc"],
        "bank_name":            r["bank_name"],
        "upi_vpa":              r["upi_vpa"],
        "is_active":            bool(r["is_active"]),
        "is_verified":          bool(r["is_verified"]),
        "verified_at":          r["verified_at"].isoformat() if r["verified_at"] else None,
        "verified_by":          str(r["verified_by"]) if r["verified_by"] else None,
        "metadata":             md or {},
        "created_at":           r["created_at"].isoformat(),
        "updated_at":           r["updated_at"].isoformat(),
    }


def _row_to_batch(r) -> dict:
    if r is None:
        return {}
    md = r["metadata"]
    if isinstance(md, str):
        md = json.loads(md)
    return {
        "id":               str(r["id"]),
        "batch_reference":  r["batch_reference"],
        "status":           r["status"],
        "total_amount":     _f(r["total_amount"]),
        "total_count":      r["total_count"],
        "currency":         r["currency"],
        "file_format":      r["file_format"],
        "file_generated_at": r["file_generated_at"].isoformat() if r["file_generated_at"] else None,
        "file_path":        r["file_path"],
        "notes":            r["notes"],
        "metadata":         md or {},
        "created_at":       r["created_at"].isoformat(),
        "created_by":       str(r["created_by"]) if r["created_by"] else None,
        "closed_at":        r["closed_at"].isoformat() if r["closed_at"] else None,
    }


class PayoutService:
    # ────────────────────────────────────────────────────────────────────
    # Beneficiaries
    # ────────────────────────────────────────────────────────────────────
    async def create_beneficiary(
        self,
        *,
        merchant_id: str | UUID,
        label: str,
        type: str,
        account_holder: Optional[str] = None,
        account_number: Optional[str] = None,
        ifsc: Optional[str] = None,
        bank_name: Optional[str] = None,
        upi_vpa: Optional[str] = None,
        metadata: Optional[dict] = None,
        created_by: Optional[str | UUID] = None,
    ) -> dict:
        if type not in ("bank", "upi"):
            raise ValidationError("type must be 'bank' or 'upi'")
        label = (label or "").strip()
        if len(label) < 2:
            raise ValidationError("label is required (min 2 chars)")

        if type == "bank":
            if not (account_number and ifsc):
                raise ValidationError("bank beneficiary requires account_number + ifsc")
            account_number = account_number.strip()
            ifsc = ifsc.strip().upper()
            last4 = account_number[-4:] if len(account_number) >= 4 else account_number
            upi_vpa = None
        else:  # upi
            if not upi_vpa or "@" not in upi_vpa:
                raise ValidationError("upi beneficiary requires a valid upi_vpa (xxx@bank)")
            upi_vpa = upi_vpa.strip().lower()
            account_number = None
            ifsc = None
            last4 = None

        async with get_transaction() as cx:
            row = await cx.fetchrow(
                """
                INSERT INTO payout_beneficiaries
                    (merchant_id, label, type, account_holder, account_number,
                     account_number_last4, ifsc, bank_name, upi_vpa, metadata, created_by)
                VALUES ($1::uuid, $2, $3::payout_beneficiary_type, $4, $5, $6, $7, $8, $9,
                        $10::jsonb, $11::uuid)
                ON CONFLICT (merchant_id, label) DO UPDATE
                  SET type           = EXCLUDED.type,
                      account_holder = EXCLUDED.account_holder,
                      account_number = EXCLUDED.account_number,
                      account_number_last4 = EXCLUDED.account_number_last4,
                      ifsc           = EXCLUDED.ifsc,
                      bank_name      = EXCLUDED.bank_name,
                      upi_vpa        = EXCLUDED.upi_vpa,
                      metadata       = EXCLUDED.metadata,
                      is_active      = true,
                      updated_at     = now()
                RETURNING *
                """,
                str(merchant_id), label, type, account_holder, account_number,
                last4, ifsc, bank_name, upi_vpa, json.dumps(metadata or {}),
                str(created_by) if created_by else None,
            )
        return _row_to_beneficiary(row)

    async def deactivate_beneficiary(
        self, *, beneficiary_id: str | UUID,
        merchant_id: Optional[str | UUID] = None,
    ) -> dict:
        clauses = ["id = $1::uuid"]
        params: list[Any] = [str(beneficiary_id)]
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        async with get_transaction() as cx:
            row = await cx.fetchrow(
                f"UPDATE payout_beneficiaries SET is_active=false, updated_at=now() "
                f"WHERE {' AND '.join(clauses)} RETURNING *",
                *params,
            )
        if row is None:
            raise NotFoundError("payout_beneficiary", str(beneficiary_id))
        return _row_to_beneficiary(row)

    async def verify_beneficiary(
        self, *, beneficiary_id: str | UUID, verified_by: str | UUID,
    ) -> dict:
        async with get_transaction() as cx:
            row = await cx.fetchrow(
                """
                UPDATE payout_beneficiaries
                   SET is_verified = true, verified_at = now(),
                       verified_by = $2::uuid, updated_at = now()
                 WHERE id = $1::uuid
             RETURNING *
                """,
                str(beneficiary_id), str(verified_by),
            )
        if row is None:
            raise NotFoundError("payout_beneficiary", str(beneficiary_id))
        return _row_to_beneficiary(row)

    async def list_beneficiaries(
        self,
        *,
        merchant_id: Optional[str | UUID] = None,
        only_active: bool = True,
        limit: int = 100,
    ) -> list[dict]:
        clauses, params = [], []
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        if only_active:
            clauses.append("is_active = true")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(min(int(limit), 500))
        async with get_connection() as c:
            rows = await c.fetch(
                f"SELECT * FROM payout_beneficiaries{where} "
                f"ORDER BY merchant_id, label LIMIT ${len(params)}",
                *params,
            )
        return [_row_to_beneficiary(r) for r in rows]

    async def get_beneficiary(
        self, *, beneficiary_id: str | UUID,
        merchant_id: Optional[str | UUID] = None,
    ) -> dict:
        clauses = ["id = $1::uuid"]
        params: list[Any] = [str(beneficiary_id)]
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        async with get_connection() as c:
            row = await c.fetchrow(
                f"SELECT * FROM payout_beneficiaries WHERE {' AND '.join(clauses)}",
                *params,
            )
        if row is None:
            raise NotFoundError("payout_beneficiary", str(beneficiary_id))
        return _row_to_beneficiary(row)

    # ────────────────────────────────────────────────────────────────────
    # Available balance
    # ────────────────────────────────────────────────────────────────────
    async def available_balance(
        self, *, merchant_id: str | UUID, currency: str = "INR",
    ) -> dict:
        async with get_connection() as c:
            avail = await c.fetchval(
                "SELECT fn_payout_available_balance($1::uuid, $2)",
                str(merchant_id), currency.upper(),
            )
            current = await c.fetchval(
                "SELECT COALESCE(current_balance, 0) "
                "FROM merchant_ledger_balance_locks "
                "WHERE merchant_id = $1::uuid AND currency = $2",
                str(merchant_id), currency.upper(),
            )
            locked = await c.fetchval(
                "SELECT COALESCE(SUM(amount), 0) FROM payout_requests "
                "WHERE merchant_id=$1::uuid AND currency=$2 "
                "AND status IN ('requested','approved','queued','processing')",
                str(merchant_id), currency.upper(),
            )
        return {
            "merchant_id":       str(merchant_id),
            "currency":          currency.upper(),
            "current_balance":   _f(current or 0),
            "in_flight_locked":  _f(locked or 0),
            "available_balance": _f(avail or 0),
        }

    # ────────────────────────────────────────────────────────────────────
    # Request payout (merchant)
    # ────────────────────────────────────────────────────────────────────
    async def request_payout(
        self,
        *,
        merchant_id: str | UUID,
        beneficiary_id: str | UUID,
        amount,
        method: str = "bank_neft",
        currency: str = "INR",
        branch_id: Optional[str | UUID] = None,
        requested_by: str | UUID,
        notes: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        if method not in ("bank_neft", "bank_imps", "bank_rtgs", "upi"):
            raise ValidationError(f"unknown method {method!r}")
        try:
            amt = Decimal(str(amount))
        except Exception:
            raise ValidationError("amount is invalid")
        if amt <= 0:
            raise ValidationError("amount must be > 0")
        currency = currency.upper()

        async with get_transaction() as cx:
            # Verify beneficiary belongs to merchant + is active
            ben = await cx.fetchrow(
                "SELECT * FROM payout_beneficiaries WHERE id=$1::uuid",
                str(beneficiary_id),
            )
            if ben is None:
                raise NotFoundError("payout_beneficiary", str(beneficiary_id))
            if str(ben["merchant_id"]) != str(merchant_id):
                raise ValidationError("beneficiary does not belong to this merchant")
            if not ben["is_active"]:
                raise ValidationError("beneficiary is inactive")
            # bank methods need a bank beneficiary; upi method needs upi
            if method.startswith("bank_") and ben["type"] != "bank":
                raise ValidationError("bank_* method requires a bank beneficiary")
            if method == "upi" and ben["type"] != "upi":
                raise ValidationError("upi method requires a upi beneficiary")

            # Idempotency replay
            if idempotency_key:
                prev = await cx.fetchrow(
                    "SELECT * FROM payout_requests "
                    "WHERE merchant_id=$1::uuid AND idempotency_key=$2",
                    str(merchant_id), idempotency_key,
                )
                if prev:
                    return _row_to_payout(prev)

            # Available balance check
            avail = await cx.fetchval(
                "SELECT fn_payout_available_balance($1::uuid, $2)",
                str(merchant_id), currency,
            )
            avail = Decimal(str(avail or 0))
            if amt > avail:
                raise ValidationError(
                    f"insufficient available balance: requested={amt} available={avail}"
                )

            # Allocate reference + insert
            ref = await cx.fetchval(
                "SELECT fn_next_payout_reference($1::uuid)", str(merchant_id),
            )
            row = await cx.fetchrow(
                """
                INSERT INTO payout_requests
                    (payout_reference, merchant_id, branch_id, beneficiary_id,
                     amount, currency, method, status, requested_by,
                     notes, idempotency_key, metadata)
                VALUES ($1, $2::uuid, $3::uuid, $4::uuid, $5, $6, $7::payout_method,
                        'requested', $8::uuid, $9, $10, $11::jsonb)
                RETURNING *
                """,
                ref, str(merchant_id),
                str(branch_id) if branch_id else None,
                str(beneficiary_id), amt, currency, method,
                str(requested_by), notes, idempotency_key,
                json.dumps(metadata or {}),
            )
            await self._emit_event(
                cx, payout_id=row["id"], event_type="created",
                from_status=None, to_status="requested",
                actor_user_id=requested_by, is_admin_action=False,
                notes=notes,
            )
        logger.info("payout.requested", extra={"payout_id": str(row["id"]),
                                                "merchant_id": str(merchant_id),
                                                "amount": _f(amt)})
        return _row_to_payout(row)

    # ────────────────────────────────────────────────────────────────────
    # Cancel (merchant — only when status='requested')
    # ────────────────────────────────────────────────────────────────────
    async def cancel_payout(
        self, *, payout_id: str | UUID, merchant_id: str | UUID,
        actor_id: str | UUID, notes: Optional[str] = None,
    ) -> dict:
        async with get_transaction() as cx:
            row = await cx.fetchrow(
                "SELECT * FROM payout_requests WHERE id=$1::uuid AND merchant_id=$2::uuid",
                str(payout_id), str(merchant_id),
            )
            if row is None:
                raise NotFoundError("payout_request", str(payout_id))
            if row["status"] != "requested":
                raise ValidationError(
                    f"only 'requested' payouts can be cancelled by merchant "
                    f"(current status: {row['status']})"
                )
            updated = await cx.fetchrow(
                """
                UPDATE payout_requests
                   SET status='cancelled', cancelled_at=now(), notes=COALESCE($2, notes)
                 WHERE id=$1::uuid
             RETURNING *
                """,
                str(payout_id), notes,
            )
            await self._emit_event(
                cx, payout_id=payout_id, event_type="cancelled",
                from_status="requested", to_status="cancelled",
                actor_user_id=actor_id, is_admin_action=False, notes=notes,
            )
        return _row_to_payout(updated)

    # ────────────────────────────────────────────────────────────────────
    # Approve / reject (admin)
    # ────────────────────────────────────────────────────────────────────
    async def approve_payout(
        self, *, payout_id: str | UUID, actor_id: str | UUID,
        notes: Optional[str] = None,
    ) -> dict:
        async with get_transaction() as cx:
            row = await cx.fetchrow(
                "SELECT * FROM payout_requests WHERE id=$1::uuid",
                str(payout_id),
            )
            if row is None:
                raise NotFoundError("payout_request", str(payout_id))
            if row["status"] != "requested":
                raise ValidationError(
                    f"only 'requested' payouts can be approved (current: {row['status']})"
                )
            updated = await cx.fetchrow(
                """
                UPDATE payout_requests
                   SET status='approved', approved_by=$2::uuid, approved_at=now()
                 WHERE id=$1::uuid
             RETURNING *
                """,
                str(payout_id), str(actor_id),
            )
            await self._emit_event(
                cx, payout_id=payout_id, event_type="approved",
                from_status="requested", to_status="approved",
                actor_user_id=actor_id, is_admin_action=True, notes=notes,
            )
        return _row_to_payout(updated)

    async def reject_payout(
        self, *, payout_id: str | UUID, actor_id: str | UUID,
        reason: str, notes: Optional[str] = None,
    ) -> dict:
        if not reason or len(reason.strip()) < 3:
            raise ValidationError("rejection reason is required (min 3 chars)")
        async with get_transaction() as cx:
            row = await cx.fetchrow(
                "SELECT * FROM payout_requests WHERE id=$1::uuid",
                str(payout_id),
            )
            if row is None:
                raise NotFoundError("payout_request", str(payout_id))
            if row["status"] not in ("requested", "approved"):
                raise ValidationError(
                    f"only 'requested' or 'approved' payouts can be rejected "
                    f"(current: {row['status']})"
                )
            from_status = row["status"]
            updated = await cx.fetchrow(
                """
                UPDATE payout_requests
                   SET status='rejected', rejected_by=$2::uuid, rejected_at=now(),
                       rejection_reason=$3
                 WHERE id=$1::uuid
             RETURNING *
                """,
                str(payout_id), str(actor_id), reason,
            )
            await self._emit_event(
                cx, payout_id=payout_id, event_type="rejected",
                from_status=from_status, to_status="rejected",
                actor_user_id=actor_id, is_admin_action=True,
                notes=notes or reason,
            )
        return _row_to_payout(updated)

    # ────────────────────────────────────────────────────────────────────
    # Batches (admin)
    # ────────────────────────────────────────────────────────────────────
    async def create_batch(
        self,
        *,
        actor_id: str | UUID,
        merchant_id: Optional[str | UUID] = None,
        payout_ids: Optional[list[str | UUID]] = None,
        currency: str = "INR",
        notes: Optional[str] = None,
    ) -> dict:
        """Create a new batch from approved payouts.

        - If ``payout_ids`` given, batch only those (must all be 'approved'
          and same currency).
        - Else, picks all 'approved' payouts (optionally filtered by merchant_id).
        """
        currency = currency.upper()
        async with get_transaction() as cx:
            if payout_ids:
                rows = await cx.fetch(
                    "SELECT * FROM payout_requests "
                    "WHERE id = ANY($1::uuid[]) FOR UPDATE",
                    [str(p) for p in payout_ids],
                )
                if len(rows) != len(payout_ids):
                    raise NotFoundError("payout_request", "one or more ids not found")
                bad = [r for r in rows if r["status"] != "approved"]
                if bad:
                    raise ValidationError(
                        f"all payouts must be 'approved'; found bad statuses: "
                        f"{[r['status'] for r in bad]}"
                    )
                bad_cur = [r for r in rows if r["currency"] != currency]
                if bad_cur:
                    raise ValidationError("all payouts in a batch must share currency")
            else:
                clauses = ["status='approved'", "batch_id IS NULL", "currency=$1"]
                params: list[Any] = [currency]
                if merchant_id is not None:
                    params.append(str(merchant_id))
                    clauses.append(f"merchant_id = ${len(params)}::uuid")
                rows = await cx.fetch(
                    f"SELECT * FROM payout_requests "
                    f"WHERE {' AND '.join(clauses)} "
                    f"ORDER BY approved_at ASC FOR UPDATE",
                    *params,
                )
            if not rows:
                raise ValidationError("no eligible 'approved' payouts to batch")

            ref = await cx.fetchval("SELECT fn_next_payout_batch_reference()")
            total = sum(Decimal(str(r["amount"])) for r in rows)
            batch = await cx.fetchrow(
                """
                INSERT INTO payout_batches
                    (batch_reference, status, total_amount, total_count, currency,
                     notes, created_by)
                VALUES ($1, 'open', $2, $3, $4, $5, $6::uuid)
                RETURNING *
                """,
                ref, total, len(rows), currency, notes, str(actor_id),
            )
            await cx.execute(
                """
                UPDATE payout_requests
                   SET status='queued', batch_id=$1::uuid
                 WHERE id = ANY($2::uuid[])
                """,
                str(batch["id"]), [str(r["id"]) for r in rows],
            )
            for r in rows:
                await self._emit_event(
                    cx, payout_id=r["id"], event_type="batched",
                    from_status="approved", to_status="queued",
                    actor_user_id=actor_id, is_admin_action=True,
                    metadata={"batch_id": str(batch["id"]),
                              "batch_reference": ref},
                )
        logger.info("payout.batch.created", extra={
            "batch_id": str(batch["id"]), "batch_reference": ref,
            "count": len(rows), "total": _f(total),
        })
        return _row_to_batch(batch)

    async def generate_batch_file(
        self, *, batch_id: str | UUID, actor_id: str | UUID,
        file_format: str = "neft_csv",
    ) -> dict:
        """Mark batch as file_generated, transition members to processing,
        and return the CSV content as text in 'file_content'."""
        if file_format not in ("neft_csv", "imps_csv", "upi_csv"):
            raise ValidationError(f"unknown file_format {file_format!r}")
        async with get_transaction() as cx:
            batch = await cx.fetchrow(
                "SELECT * FROM payout_batches WHERE id=$1::uuid FOR UPDATE",
                str(batch_id),
            )
            if batch is None:
                raise NotFoundError("payout_batch", str(batch_id))
            if batch["status"] not in ("open", "file_generated"):
                raise ValidationError(
                    f"cannot regenerate: batch status is {batch['status']}"
                )
            members = await cx.fetch(
                """
                SELECT pr.*, pb.label AS ben_label, pb.type AS ben_type,
                       pb.account_holder, pb.account_number, pb.ifsc,
                       pb.bank_name, pb.upi_vpa
                  FROM payout_requests pr
                  JOIN payout_beneficiaries pb ON pb.id = pr.beneficiary_id
                 WHERE pr.batch_id = $1::uuid
                 ORDER BY pr.requested_at
                """,
                str(batch_id),
            )
            # Build CSV
            buf = io.StringIO()
            w = csv.writer(buf)
            if file_format == "upi_csv":
                w.writerow(["payout_reference", "vpa", "amount", "currency", "narration"])
                for m in members:
                    w.writerow([m["payout_reference"], m["upi_vpa"],
                                f"{Decimal(str(m['amount'])):.2f}",
                                m["currency"],
                                (m["notes"] or m["payout_reference"])[:100]])
            else:
                # NEFT / IMPS share same envelope
                w.writerow([
                    "payout_reference", "beneficiary_name", "account_number",
                    "ifsc", "bank_name", "amount", "currency", "narration",
                ])
                for m in members:
                    w.writerow([
                        m["payout_reference"],
                        m["account_holder"] or "",
                        m["account_number"] or "",
                        m["ifsc"] or "",
                        m["bank_name"] or "",
                        f"{Decimal(str(m['amount'])):.2f}",
                        m["currency"],
                        (m["notes"] or m["payout_reference"])[:100],
                    ])
            file_content = buf.getvalue()

            updated_batch = await cx.fetchrow(
                """
                UPDATE payout_batches
                   SET status='file_generated', file_format=$2,
                       file_generated_at=now()
                 WHERE id=$1::uuid
             RETURNING *
                """,
                str(batch_id), file_format,
            )
            await cx.execute(
                "UPDATE payout_requests SET status='processing' "
                "WHERE batch_id=$1::uuid AND status='queued'",
                str(batch_id),
            )
            for m in members:
                if m["status"] == "queued":
                    await self._emit_event(
                        cx, payout_id=m["id"], event_type="file_generated",
                        from_status="queued", to_status="processing",
                        actor_user_id=actor_id, is_admin_action=True,
                    )
        return {
            "batch":        _row_to_batch(updated_batch),
            "file_format":  file_format,
            "file_name":    f"{updated_batch['batch_reference']}.csv",
            "file_content": file_content,
            "row_count":    len(members),
        }

    async def list_batches(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        clauses, params = [], []
        if status:
            params.append(status)
            clauses.append(f"status = ${len(params)}::payout_batch_status")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(min(int(limit), 200))
        async with get_connection() as c:
            rows = await c.fetch(
                f"SELECT * FROM payout_batches{where} "
                f"ORDER BY created_at DESC LIMIT ${len(params)}",
                *params,
            )
        return [_row_to_batch(r) for r in rows]

    async def get_batch(self, batch_id: str | UUID) -> dict:
        async with get_connection() as c:
            row = await c.fetchrow(
                "SELECT * FROM payout_batches WHERE id=$1::uuid", str(batch_id),
            )
        if row is None:
            raise NotFoundError("payout_batch", str(batch_id))
        return _row_to_batch(row)

    async def list_batch_payouts(self, batch_id: str | UUID) -> list[dict]:
        async with get_connection() as c:
            rows = await c.fetch(
                "SELECT * FROM payout_requests WHERE batch_id=$1::uuid "
                "ORDER BY requested_at",
                str(batch_id),
            )
        return [_row_to_payout(r) for r in rows]

    # ────────────────────────────────────────────────────────────────────
    # mark_sent — debits merchant_ledger
    # ────────────────────────────────────────────────────────────────────
    async def mark_sent(
        self,
        *,
        payout_id: str | UUID,
        actor_id: str | UUID,
        utr_number: Optional[str] = None,
        bank_reference: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> dict:
        async with get_transaction() as cx:
            row = await cx.fetchrow(
                "SELECT * FROM payout_requests WHERE id=$1::uuid FOR UPDATE",
                str(payout_id),
            )
            if row is None:
                raise NotFoundError("payout_request", str(payout_id))
            if row["status"] != "processing":
                raise ValidationError(
                    f"mark_sent requires status='processing' (current: {row['status']})"
                )
            # Post merchant_ledger debit
            ledger_json = await cx.fetchval(
                """
                SELECT fn_post_merchant_ledger_entry(
                    $1::uuid, $2::uuid, 'payout_initiated'::merchant_ledger_txn_type,
                    $3, 0, $4,
                    'payout', $5::uuid, NULL, NULL, NULL,
                    $6, $7, $8, $9::jsonb, $10::uuid
                )
                """,
                str(row["merchant_id"]),
                str(row["branch_id"]) if row["branch_id"] else None,
                Decimal(str(row["amount"])),
                row["currency"],
                str(payout_id),
                bank_reference,
                utr_number,
                f"payout-sent:{row['payout_reference']}",
                json.dumps({
                    "payout_reference": row["payout_reference"],
                    "method": row["method"],
                    "beneficiary_id": str(row["beneficiary_id"]),
                }),
                str(actor_id),
            )
            ledger_data = ledger_json if isinstance(ledger_json, dict) else json.loads(ledger_json)
            ledger_id = ledger_data["id"]

            updated = await cx.fetchrow(
                """
                UPDATE payout_requests
                   SET status='sent', sent_at=now(),
                       utr_number=COALESCE($2, utr_number),
                       bank_reference=COALESCE($3, bank_reference),
                       ledger_entry_id=$4::uuid,
                       notes=COALESCE($5, notes)
                 WHERE id=$1::uuid
             RETURNING *
                """,
                str(payout_id), utr_number, bank_reference,
                ledger_id, notes,
            )
            await self._emit_event(
                cx, payout_id=payout_id, event_type="sent",
                from_status="processing", to_status="sent",
                actor_user_id=actor_id, is_admin_action=True, notes=notes,
                metadata={"ledger_entry_id": ledger_id, "utr": utr_number},
            )
        logger.info("payout.sent", extra={
            "payout_id": str(payout_id),
            "ledger_id": ledger_id,
            "utr": utr_number,
        })
        return _row_to_payout(updated)

    # ────────────────────────────────────────────────────────────────────
    # mark_completed — terminal happy path
    # ────────────────────────────────────────────────────────────────────
    async def mark_completed(
        self, *, payout_id: str | UUID, actor_id: str | UUID,
        utr_number: Optional[str] = None, notes: Optional[str] = None,
    ) -> dict:
        async with get_transaction() as cx:
            row = await cx.fetchrow(
                "SELECT * FROM payout_requests WHERE id=$1::uuid",
                str(payout_id),
            )
            if row is None:
                raise NotFoundError("payout_request", str(payout_id))
            if row["status"] != "sent":
                raise ValidationError(
                    f"mark_completed requires status='sent' (current: {row['status']})"
                )
            updated = await cx.fetchrow(
                """
                UPDATE payout_requests
                   SET status='completed', completed_at=now(),
                       utr_number=COALESCE($2, utr_number),
                       notes=COALESCE($3, notes)
                 WHERE id=$1::uuid
             RETURNING *
                """,
                str(payout_id), utr_number, notes,
            )
            await self._emit_event(
                cx, payout_id=payout_id, event_type="completed",
                from_status="sent", to_status="completed",
                actor_user_id=actor_id, is_admin_action=True, notes=notes,
                metadata={"utr": utr_number},
            )
        return _row_to_payout(updated)

    # ────────────────────────────────────────────────────────────────────
    # mark_failed — credit ledger back if it was 'sent'
    # ────────────────────────────────────────────────────────────────────
    async def mark_failed(
        self,
        *,
        payout_id: str | UUID,
        actor_id: str | UUID,
        reason: str,
        notes: Optional[str] = None,
    ) -> dict:
        if not reason or len(reason.strip()) < 3:
            raise ValidationError("failure reason is required (min 3 chars)")
        async with get_transaction() as cx:
            row = await cx.fetchrow(
                "SELECT * FROM payout_requests WHERE id=$1::uuid FOR UPDATE",
                str(payout_id),
            )
            if row is None:
                raise NotFoundError("payout_request", str(payout_id))
            if row["status"] not in ("queued", "processing", "sent"):
                raise ValidationError(
                    f"mark_failed only valid from 'queued'|'processing'|'sent' "
                    f"(current: {row['status']})"
                )
            from_status = row["status"]
            reversal_id = None
            if from_status == "sent":
                # Credit back the merchant
                rev_json = await cx.fetchval(
                    """
                    SELECT fn_post_merchant_ledger_entry(
                        $1::uuid, $2::uuid,
                        'payout_reversed'::merchant_ledger_txn_type,
                        0, $3, $4,
                        'payout_reversal', $5::uuid, NULL, NULL, NULL,
                        NULL, NULL, $6, $7::jsonb, $8::uuid
                    )
                    """,
                    str(row["merchant_id"]),
                    str(row["branch_id"]) if row["branch_id"] else None,
                    Decimal(str(row["amount"])), row["currency"],
                    str(payout_id),
                    f"payout-failed:{row['payout_reference']}",
                    json.dumps({
                        "payout_reference": row["payout_reference"],
                        "original_ledger_entry_id":
                            str(row["ledger_entry_id"]) if row["ledger_entry_id"] else None,
                        "reason": reason,
                    }),
                    str(actor_id),
                )
                rev_data = rev_json if isinstance(rev_json, dict) else json.loads(rev_json)
                reversal_id = rev_data["id"]

            updated = await cx.fetchrow(
                """
                UPDATE payout_requests
                   SET status='failed', failed_at=now(),
                       failure_reason=$2,
                       reversal_entry_id=$3::uuid,
                       notes=COALESCE($4, notes)
                 WHERE id=$1::uuid
             RETURNING *
                """,
                str(payout_id), reason, reversal_id, notes,
            )
            await self._emit_event(
                cx, payout_id=payout_id, event_type="failed",
                from_status=from_status, to_status="failed",
                actor_user_id=actor_id, is_admin_action=True,
                notes=notes or reason,
                metadata={"reversal_entry_id": reversal_id, "reason": reason},
            )
            if reversal_id:
                await self._emit_event(
                    cx, payout_id=payout_id, event_type="reversed",
                    from_status="failed", to_status="failed",
                    actor_user_id=actor_id, is_admin_action=True,
                    metadata={"reversal_entry_id": reversal_id},
                )
        logger.warning("payout.failed", extra={
            "payout_id": str(payout_id),
            "from_status": from_status,
            "reversal_entry_id": reversal_id,
        })
        return _row_to_payout(updated)

    # ────────────────────────────────────────────────────────────────────
    # Reads
    # ────────────────────────────────────────────────────────────────────
    async def list_payouts(
        self,
        *,
        merchant_id: Optional[str | UUID] = None,
        status: Optional[str] = None,
        beneficiary_id: Optional[str | UUID] = None,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        limit: int = 50,
    ) -> list[dict]:
        clauses, params = [], []
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        if status:
            params.append(status)
            clauses.append(f"status = ${len(params)}::payout_status")
        if beneficiary_id:
            params.append(str(beneficiary_id))
            clauses.append(f"beneficiary_id = ${len(params)}::uuid")
        if from_date:
            params.append(from_date)
            clauses.append(f"requested_at >= ${len(params)}")
        if to_date:
            params.append(to_date)
            clauses.append(f"requested_at <= ${len(params)}")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(min(int(limit), 500))
        async with get_connection() as c:
            rows = await c.fetch(
                f"SELECT * FROM payout_requests{where} "
                f"ORDER BY requested_at DESC LIMIT ${len(params)}",
                *params,
            )
        return [_row_to_payout(r) for r in rows]

    async def get_payout(
        self, *, payout_id: str | UUID,
        merchant_id: Optional[str | UUID] = None,
    ) -> dict:
        clauses = ["id = $1::uuid"]
        params: list[Any] = [str(payout_id)]
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        async with get_connection() as c:
            row = await c.fetchrow(
                f"SELECT * FROM payout_requests WHERE {' AND '.join(clauses)}",
                *params,
            )
        if row is None:
            raise NotFoundError("payout_request", str(payout_id))
        return _row_to_payout(row)

    async def list_events(self, payout_id: str | UUID) -> list[dict]:
        async with get_connection() as c:
            rows = await c.fetch(
                "SELECT * FROM payout_status_events "
                "WHERE payout_id=$1::uuid ORDER BY created_at",
                str(payout_id),
            )
        out = []
        for r in rows:
            md = r["metadata"]
            if isinstance(md, str):
                md = json.loads(md)
            out.append({
                "id":              str(r["id"]),
                "payout_id":       str(r["payout_id"]),
                "event_type":      r["event_type"],
                "from_status":     r["from_status"],
                "to_status":       r["to_status"],
                "actor_user_id":   str(r["actor_user_id"]) if r["actor_user_id"] else None,
                "is_admin_action": bool(r["is_admin_action"]),
                "notes":           r["notes"],
                "metadata":        md or {},
                "created_at":      r["created_at"].isoformat(),
            })
        return out

    async def get_summary(
        self, *, merchant_id: Optional[str | UUID] = None,
    ) -> dict:
        clauses, params = [], []
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        async with get_connection() as c:
            rows = await c.fetch(
                f"SELECT status, COUNT(*) AS cnt, COALESCE(SUM(amount),0) AS total "
                f"FROM payout_requests{where} GROUP BY status",
                *params,
            )
        by_status = {r["status"]: {"count": r["cnt"], "total": _f(r["total"])}
                     for r in rows}
        return {
            "merchant_id": str(merchant_id) if merchant_id else None,
            "by_status":   by_status,
            "totals": {
                "all_count": sum(v["count"] for v in by_status.values()),
                "all_amount": sum(v["total"] or 0 for v in by_status.values()),
                "in_flight_count": sum(
                    by_status.get(s, {}).get("count", 0)
                    for s in PENDING_STATUSES
                ),
                "in_flight_amount": sum(
                    by_status.get(s, {}).get("total", 0) or 0
                    for s in PENDING_STATUSES
                ),
            },
        }

    async def admin_summary_by_merchant(self) -> list[dict]:
        async with get_connection() as c:
            rows = await c.fetch(
                """
                SELECT merchant_id,
                       COUNT(*) AS total_count,
                       COALESCE(SUM(amount),0) AS total_amount,
                       COUNT(*) FILTER (
                           WHERE status IN ('requested','approved','queued','processing')
                       ) AS in_flight_count,
                       COALESCE(SUM(amount) FILTER (
                           WHERE status IN ('requested','approved','queued','processing')
                       ), 0) AS in_flight_amount,
                       COUNT(*) FILTER (WHERE status='completed') AS completed_count,
                       COALESCE(SUM(amount) FILTER (WHERE status='completed'), 0)
                           AS completed_amount,
                       COUNT(*) FILTER (WHERE status='failed') AS failed_count
                  FROM payout_requests
              GROUP BY merchant_id
              ORDER BY total_amount DESC
                """
            )
        return [
            {
                "merchant_id":      str(r["merchant_id"]),
                "total_count":      r["total_count"],
                "total_amount":     _f(r["total_amount"]),
                "in_flight_count":  r["in_flight_count"],
                "in_flight_amount": _f(r["in_flight_amount"]),
                "completed_count":  r["completed_count"],
                "completed_amount": _f(r["completed_amount"]),
                "failed_count":     r["failed_count"],
            }
            for r in rows
        ]

    # ────────────────────────────────────────────────────────────────────
    # Internal: emit event
    # ────────────────────────────────────────────────────────────────────
    async def _emit_event(
        self,
        cx,
        *,
        payout_id,
        event_type: str,
        from_status: Optional[str],
        to_status: Optional[str],
        actor_user_id,
        is_admin_action: bool,
        notes: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        await cx.execute(
            """
            INSERT INTO payout_status_events
                (payout_id, event_type, from_status, to_status,
                 actor_user_id, is_admin_action, notes, metadata)
            VALUES ($1::uuid, $2::payout_event_type,
                    $3::payout_status, $4::payout_status,
                    $5::uuid, $6, $7, $8::jsonb)
            """,
            str(payout_id), event_type, from_status, to_status,
            str(actor_user_id) if actor_user_id else None,
            is_admin_action, notes, json.dumps(metadata or {}),
        )


payout_service = PayoutService()
