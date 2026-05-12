"""
Merchant Statement service — Phase 5 (period-based ledger statements).

Generates monthly (or arbitrary period) statements for a merchant by reading
the ``merchant_ledger`` table populated by Phase 1-4. NEVER posts ledger
entries — read-only over the bookkeeping engine.

NOTE: distinct from ``app.services.statement_service`` which manages the
gateway-style settlement batches. This service produces simple period
statements (opening balance, credits, debits, closing balance, breakdown).

Lifecycle:

    generate() ──→ ready ──cancel→ cancelled

Single-call ``generate()`` is sufficient because computation is deterministic
and cheap (one window query); we don't need a two-phase ``generating → ready``.

Admin/merchant separation
─────────────────────────
    • Merchant: generate own statements, list/get/download own.
    • Admin:    generate for any merchant, list cross-merchant, cancel.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from app.core.database import get_connection, get_transaction
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)


def _f(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(round(v, 4))
    return float(v)


def _row_to_statement(r) -> dict:
    if r is None:
        return {}
    md = r["metadata"]
    if isinstance(md, str):
        md = json.loads(md)
    bd = r["breakdown"]
    if isinstance(bd, str):
        bd = json.loads(bd)
    return {
        "id":               str(r["id"]),
        "merchant_id":      str(r["merchant_id"]),
        "period_start":     r["period_start"].isoformat(),
        "period_end":       r["period_end"].isoformat(),
        "currency":         r["currency"],
        "opening_balance":  _f(r["opening_balance"]),
        "total_credits":    _f(r["total_credits"]),
        "total_debits":     _f(r["total_debits"]),
        "closing_balance":  _f(r["closing_balance"]),
        "txn_count":        int(r["txn_count"] or 0),
        "breakdown":        bd or {},
        "status":           r["status"],
        "file_format":      r["file_format"],
        "file_path":        r["file_path"],
        "notes":            r["notes"],
        "metadata":         md or {},
        "generated_at":     r["generated_at"].isoformat(),
        "generated_by":     str(r["generated_by"]) if r["generated_by"] else None,
        "cancelled_at":     r["cancelled_at"].isoformat() if r["cancelled_at"] else None,
        "cancelled_by":     str(r["cancelled_by"]) if r["cancelled_by"] else None,
        "cancellation_reason": r["cancellation_reason"],
    }


def _row_to_entry(r) -> dict:
    md = r["metadata"]
    if isinstance(md, str):
        md = json.loads(md)
    return {
        "id":                str(r["id"]),
        "merchant_id":       str(r["merchant_id"]),
        "branch_id":         str(r["branch_id"]) if r["branch_id"] else None,
        "ledger_reference":  r["ledger_reference"],
        "transaction_type":  r["transaction_type"],
        "source_type":       r["source_type"],
        "source_id":         str(r["source_id"]) if r["source_id"] else None,
        "settlement_id":     str(r["settlement_id"]) if r["settlement_id"] else None,
        "payment_id":        str(r["payment_id"]) if r["payment_id"] else None,
        "order_id":          str(r["order_id"]) if r["order_id"] else None,
        "debit_amount":      _f(r["debit_amount"]),
        "credit_amount":     _f(r["credit_amount"]),
        "balance_after":     _f(r["balance_after"]),
        "currency":          r["currency"],
        "bank_reference":    r["bank_reference"],
        "utr_number":        r["utr_number"],
        "metadata":          md or {},
        "created_at":        r["created_at"].isoformat(),
    }


class MerchantStatementService:
    # ────────────────────────────────────────────────────────────────────
    # Generate
    # ────────────────────────────────────────────────────────────────────
    async def generate(
        self,
        *,
        merchant_id: str | UUID,
        period_start: datetime,
        period_end:   datetime,
        currency: str = "INR",
        generated_by: Optional[str | UUID] = None,
        notes: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        if period_end <= period_start:
            raise ValidationError("period_end must be > period_start")
        cur = (currency or "INR").upper()
        async with get_transaction() as cx:
            comp = await cx.fetchrow(
                "SELECT * FROM fn_compute_statement($1::uuid, $2, $3, $4)",
                str(merchant_id), period_start, period_end, cur,
            )
            bd = comp["breakdown"]
            if isinstance(bd, str):
                bd = json.loads(bd or "{}")
            row = await cx.fetchrow(
                """
                INSERT INTO merchant_statements
                    (merchant_id, period_start, period_end, currency,
                     opening_balance, total_credits, total_debits, closing_balance,
                     txn_count, breakdown, status, file_format,
                     notes, metadata, generated_by)
                VALUES ($1::uuid, $2, $3, $4,
                        $5, $6, $7, $8,
                        $9, $10::jsonb, 'ready', 'csv',
                        $11, $12::jsonb, $13::uuid)
                RETURNING *
                """,
                str(merchant_id), period_start, period_end, cur,
                comp["opening_balance"] or Decimal("0"),
                comp["total_credits"]   or Decimal("0"),
                comp["total_debits"]    or Decimal("0"),
                comp["closing_balance"] or (comp["opening_balance"] or Decimal("0")),
                int(comp["txn_count"] or 0),
                json.dumps(bd or {}),
                notes, json.dumps(metadata or {}),
                str(generated_by) if generated_by else None,
            )
        logger.info("merchant_statement.generated", extra={
            "merchant_id": str(merchant_id),
            "statement_id": str(row["id"]),
            "txn_count":  int(comp["txn_count"] or 0),
        })
        return _row_to_statement(row)

    # ────────────────────────────────────────────────────────────────────
    # Reads
    # ────────────────────────────────────────────────────────────────────
    async def list_statements(
        self,
        *,
        merchant_id: Optional[str | UUID] = None,
        from_date: Optional[datetime] = None,
        to_date:   Optional[datetime] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        clauses, params = [], []
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        if status:
            params.append(status)
            clauses.append(f"status = ${len(params)}::statement_status")
        if from_date:
            params.append(from_date)
            clauses.append(f"period_end >= ${len(params)}")
        if to_date:
            params.append(to_date)
            clauses.append(f"period_start <= ${len(params)}")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(min(int(limit), 500))
        async with get_connection() as c:
            rows = await c.fetch(
                f"SELECT * FROM merchant_statements{where} "
                f"ORDER BY period_end DESC LIMIT ${len(params)}",
                *params,
            )
        return [_row_to_statement(r) for r in rows]

    async def get_statement(
        self,
        *,
        statement_id: str | UUID,
        merchant_id: Optional[str | UUID] = None,
    ) -> dict:
        clauses = ["id = $1::uuid"]
        params: list[Any] = [str(statement_id)]
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        async with get_connection() as c:
            row = await c.fetchrow(
                f"SELECT * FROM merchant_statements WHERE {' AND '.join(clauses)}",
                *params,
            )
        if row is None:
            raise NotFoundError("merchant_statement", str(statement_id))
        return _row_to_statement(row)

    async def list_entries(
        self,
        *,
        statement_id: str | UUID,
        merchant_id: Optional[str | UUID] = None,
        limit: int = 1000,
    ) -> list[dict]:
        clauses = ["id = $1::uuid"]
        params: list[Any] = [str(statement_id)]
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        async with get_connection() as c:
            st_row = await c.fetchrow(
                "SELECT merchant_id, currency, period_start, period_end "
                f"FROM merchant_statements WHERE {' AND '.join(clauses)}",
                *params,
            )
            if st_row is None:
                raise NotFoundError("merchant_statement", str(statement_id))
            rows = await c.fetch(
                """
                SELECT * FROM merchant_ledger
                 WHERE merchant_id = $1::uuid
                   AND currency    = $2
                   AND created_at >= $3
                   AND created_at <  $4
                 ORDER BY created_at ASC, id ASC
                 LIMIT $5
                """,
                st_row["merchant_id"], st_row["currency"],
                st_row["period_start"], st_row["period_end"],
                min(int(limit), 5000),
            )
        return [_row_to_entry(r) for r in rows]

    # ────────────────────────────────────────────────────────────────────
    # Cancel
    # ────────────────────────────────────────────────────────────────────
    async def cancel(
        self,
        *,
        statement_id: str | UUID,
        actor_id: str | UUID,
        reason: str,
    ) -> dict:
        if not reason or len(reason.strip()) < 3:
            raise ValidationError("cancellation reason is required (min 3 chars)")
        async with get_transaction() as cx:
            st = await cx.fetchrow(
                "SELECT status FROM merchant_statements WHERE id=$1::uuid FOR UPDATE",
                str(statement_id),
            )
            if st is None:
                raise NotFoundError("merchant_statement", str(statement_id))
            if st["status"] != "ready":
                raise ValidationError(
                    f"cannot cancel statement in status {st['status']!r}"
                )
            row = await cx.fetchrow(
                """
                UPDATE merchant_statements
                   SET status='cancelled', cancelled_at=now(),
                       cancelled_by=$2::uuid, cancellation_reason=$3
                 WHERE id=$1::uuid
             RETURNING *
                """,
                str(statement_id), str(actor_id), reason,
            )
        return _row_to_statement(row)

    # ────────────────────────────────────────────────────────────────────
    # CSV download
    # ────────────────────────────────────────────────────────────────────
    async def to_csv(
        self,
        *,
        statement_id: str | UUID,
        merchant_id: Optional[str | UUID] = None,
    ) -> dict:
        st = await self.get_statement(
            statement_id=statement_id, merchant_id=merchant_id,
        )
        entries = await self.list_entries(
            statement_id=statement_id, merchant_id=merchant_id, limit=5000,
        )
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Merchant Statement"])
        w.writerow(["Statement ID", st["id"]])
        w.writerow(["Merchant ID",  st["merchant_id"]])
        w.writerow(["Period Start", st["period_start"]])
        w.writerow(["Period End",   st["period_end"]])
        w.writerow(["Currency",     st["currency"]])
        w.writerow(["Opening Balance", f"{st['opening_balance']:.2f}"])
        w.writerow(["Total Credits",   f"{st['total_credits']:.2f}"])
        w.writerow(["Total Debits",    f"{st['total_debits']:.2f}"])
        w.writerow(["Closing Balance", f"{st['closing_balance']:.2f}"])
        w.writerow(["Txn Count", st["txn_count"]])
        w.writerow([])
        w.writerow([
            "Date", "Ledger Ref", "Source Type", "Source ID", "Type",
            "Debit", "Credit", "Balance After", "UTR", "Bank Ref",
        ])
        for e in entries:
            w.writerow([
                e["created_at"], e["ledger_reference"] or "",
                e["source_type"] or "", e["source_id"] or "",
                e["transaction_type"],
                f"{e['debit_amount']:.2f}", f"{e['credit_amount']:.2f}",
                f"{e['balance_after']:.2f}",
                e["utr_number"] or "", e["bank_reference"] or "",
            ])
        w.writerow([])
        w.writerow(["Total", "", "", "", "",
                    f"{st['total_debits']:.2f}",
                    f"{st['total_credits']:.2f}",
                    f"{st['closing_balance']:.2f}"])
        fname = (
            f"statement-{st['merchant_id'][:8]}-"
            f"{st['period_start'][:10]}_{st['period_end'][:10]}.csv"
        )
        return {
            "statement":    st,
            "file_name":    fname,
            "file_content": buf.getvalue(),
        }


merchant_statement_service = MerchantStatementService()
