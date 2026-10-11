"""Frozen period statements using the same Route balance projection as wallets.

New statements store their entry snapshot in metadata in the same transaction
as their summary. Legacy statements remain labelled with their original basis.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from app.core.database import get_connection, get_transaction
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.services.razorpay.merchant_finance_service import merchant_finance_service as finance

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
        "id": str(r["id"]),
        "merchant_id": str(r["merchant_id"]),
        "period_start": r["period_start"].isoformat(),
        "period_end": r["period_end"].isoformat(),
        "currency": r["currency"],
        "opening_balance": _f(r["opening_balance"]),
        "total_credits": _f(r["total_credits"]),
        "total_debits": _f(r["total_debits"]),
        "closing_balance": _f(r["closing_balance"]),
        "txn_count": int(r["txn_count"] or 0),
        "breakdown": bd or {},
        "status": r["status"],
        "file_format": r["file_format"],
        "file_path": r["file_path"],
        "notes": r["notes"],
        "metadata": {k: v for k, v in (md or {}).items() if k != "_entries"},
        "source": (md or {}).get("financial_source", "legacy_merchant_ledger"),
        "entries_frozen": "_entries" in (md or {}),
        "generated_at": r["generated_at"].isoformat(),
        "generated_by": str(r["generated_by"]) if r["generated_by"] else None,
        "cancelled_at": r["cancelled_at"].isoformat() if r["cancelled_at"] else None,
        "cancelled_by": str(r["cancelled_by"]) if r["cancelled_by"] else None,
        "cancellation_reason": r["cancellation_reason"],
    }


def _row_to_entry(r) -> dict:
    md = r["metadata"]
    if isinstance(md, str):
        md = json.loads(md)
    return {
        "id": str(r["id"]),
        "merchant_id": str(r["merchant_id"]),
        "branch_id": str(r["branch_id"]) if r["branch_id"] else None,
        "ledger_reference": r["ledger_reference"],
        "transaction_type": r["transaction_type"],
        "source_type": r["source_type"],
        "source_id": str(r["source_id"]) if r["source_id"] else None,
        "settlement_id": str(r["settlement_id"]) if r["settlement_id"] else None,
        "payment_id": str(r["payment_id"]) if r["payment_id"] else None,
        "order_id": str(r["order_id"]) if r["order_id"] else None,
        "debit_amount": _f(r["debit_amount"]),
        "credit_amount": _f(r["credit_amount"]),
        "balance_after": _f(r["balance_after"]),
        "currency": r["currency"],
        "bank_reference": r["bank_reference"],
        "utr_number": r["utr_number"],
        "metadata": md or {},
        "created_at": r["created_at"].isoformat(),
    }


class MerchantStatementService:
    # ────────────────────────────────────────────────────────────────────
    # Generate
    # ────────────────────────────────────────────────────────────────────
    async def generate(
        self, *, merchant_id, period_start, period_end, currency="INR", generated_by=None, notes=None, metadata=None
    ) -> dict:
        period_start = period_start.replace(tzinfo=timezone.utc) if period_start.tzinfo is None else period_start
        period_end = period_end.replace(tzinfo=timezone.utc) if period_end.tzinfo is None else period_end
        if period_end <= period_start:
            raise ValidationError("period_end must be > period_start")
        if currency.upper() != "INR":
            raise ValidationError("Route statements currently support INR only")
        async with get_transaction() as cx:
            await cx.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            stream = await finance._build_entries(
                str(merchant_id),
                from_date=None,
                to_date=period_end.astimezone(ZoneInfo("Asia/Kolkata")).date(),
                conn=cx,
            )
            prior = []
            selected = []
            for entry in stream:
                at = datetime.fromisoformat(entry["at"])
                if at >= period_end:
                    continue
                if entry.get("allocation_status") == "unresolved":
                    raise ValidationError(
                        "Reconcile missing historical Route allocations before generating a statement."
                    )
                (prior if at < period_start else selected).append(entry)
            opening = Decimal(str(prior[-1]["balance_after"])) if prior else Decimal("0")
            credits = debits = Decimal("0")
            breakdown = {}
            frozen = []
            for entry in selected:
                amount = Decimal(str(entry["amount"])) if entry["affects_balance"] else Decimal("0")
                credit = amount if entry["type"] == "CREDIT" else Decimal("0")
                debit = amount if entry["type"] == "DEBIT" else Decimal("0")
                credits += credit
                debits += debit
                group = breakdown.setdefault(entry["entry_type"], {"credit": 0, "debit": 0, "count": 0})
                group["credit"] += float(credit)
                group["debit"] += float(debit)
                group["count"] += 1
                frozen.append(
                    {
                        "id": entry["entry_id"],
                        "merchant_id": str(merchant_id),
                        "branch_id": None,
                        "ledger_reference": entry["entry_id"],
                        "transaction_type": entry["entry_type"],
                        "source_type": entry["source"],
                        "source_id": entry["reference"],
                        "settlement_id": entry["reference"] if entry["source"] == "settlement" else None,
                        "payment_id": entry.get("payment_id")
                        or (entry["reference"] if entry["source"] == "payment" else None),
                        "order_id": entry.get("order_id"),
                        "debit_amount": float(debit),
                        "credit_amount": float(credit),
                        "balance_after": entry["balance_after"],
                        "currency": "INR",
                        "bank_reference": None,
                        "utr_number": entry.get("utr"),
                        "metadata": {"amount": entry["amount"], "affects_balance": entry["affects_balance"]},
                        "created_at": entry["at"],
                    }
                )
            md = {
                **(metadata or {}),
                "financial_source": "razorpay_route_mirror",
                "projection_version": 2,
                "balance_basis": "online_route_entitlement",
                "_entries": frozen,
            }
            row = await cx.fetchrow(
                """
                INSERT INTO merchant_statements
                  (merchant_id,period_start,period_end,currency,opening_balance,total_credits,total_debits,
                   closing_balance,txn_count,breakdown,status,file_format,notes,metadata,generated_by)
                VALUES($1::uuid,$2,$3,'INR',$4,$5,$6,$7,$8,$9::jsonb,'ready','csv',$10,$11::jsonb,$12::uuid)
                RETURNING *
            """,
                str(merchant_id),
                period_start,
                period_end,
                opening,
                credits,
                debits,
                opening + credits - debits,
                len(frozen),
                json.dumps(breakdown),
                notes,
                json.dumps(md),
                str(generated_by) if generated_by else None,
            )
        return _row_to_statement(row)

    # ────────────────────────────────────────────────────────────────────
    # Reads
    # ────────────────────────────────────────────────────────────────────
    async def list_statements(
        self,
        *,
        merchant_id: Optional[str | UUID] = None,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
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
                f"SELECT * FROM merchant_statements{where} ORDER BY period_end DESC LIMIT ${len(params)}",
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

    async def list_entries(self, *, statement_id, merchant_id=None, limit=1000) -> list[dict]:
        if limit is not None and limit < 1:
            raise ValidationError("limit must be positive")
        params = [str(statement_id)]
        where = "id=$1::uuid"
        if merchant_id is not None:
            params.append(str(merchant_id))
            where += " AND merchant_id=$2::uuid"
        async with get_connection() as c:
            st = await c.fetchrow(
                "SELECT merchant_id,currency,period_start,period_end,metadata FROM merchant_statements WHERE " + where,
                *params,
            )
            if st is None:
                raise NotFoundError("merchant_statement", str(statement_id))
            md = st["metadata"]
            md = json.loads(md) if isinstance(md, str) else (md or {})
            if "_entries" in md:
                return md["_entries"] if limit is None else md["_entries"][:limit]
            # Historical statements retain their original ledger basis, explicitly labelled by get_statement.
            rows = await c.fetch(
                """
                SELECT * FROM merchant_ledger WHERE merchant_id=$1::uuid AND currency=$2
                  AND created_at >= $3 AND created_at < $4 ORDER BY created_at,id LIMIT $5
            """,
                st["merchant_id"],
                st["currency"],
                st["period_start"],
                st["period_end"],
                limit,
            )
        return [_row_to_entry(row) for row in rows]

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
                raise ValidationError(f"cannot cancel statement in status {st['status']!r}")
            row = await cx.fetchrow(
                """
                UPDATE merchant_statements
                   SET status='cancelled', cancelled_at=now(),
                       cancelled_by=$2::uuid, cancellation_reason=$3
                 WHERE id=$1::uuid
             RETURNING *
                """,
                str(statement_id),
                str(actor_id),
                reason,
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
            statement_id=statement_id,
            merchant_id=merchant_id,
        )
        entries = await self.list_entries(
            statement_id=statement_id,
            merchant_id=merchant_id,
            limit=None,
        )
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Merchant Statement"])
        w.writerow(["Statement ID", st["id"]])
        w.writerow(["Merchant ID", st["merchant_id"]])
        w.writerow(["Period Start", st["period_start"]])
        w.writerow(["Period End", st["period_end"]])
        w.writerow(["Currency", st["currency"]])
        w.writerow(["Opening Balance", f"{st['opening_balance']:.2f}"])
        w.writerow(["Total Credits", f"{st['total_credits']:.2f}"])
        w.writerow(["Total Debits", f"{st['total_debits']:.2f}"])
        w.writerow(["Closing Balance", f"{st['closing_balance']:.2f}"])
        w.writerow(["Txn Count", st["txn_count"]])
        w.writerow([])
        w.writerow(
            [
                "Date",
                "Ledger Ref",
                "Source Type",
                "Source ID",
                "Type",
                "Debit",
                "Credit",
                "Balance After",
                "UTR",
                "Bank Ref",
            ]
        )
        for e in entries:
            w.writerow(
                [
                    e["created_at"],
                    e["ledger_reference"] or "",
                    e["source_type"] or "",
                    e["source_id"] or "",
                    e["transaction_type"],
                    f"{e['debit_amount']:.2f}",
                    f"{e['credit_amount']:.2f}",
                    f"{e['balance_after']:.2f}",
                    e["utr_number"] or "",
                    e["bank_reference"] or "",
                ]
            )
        w.writerow([])
        w.writerow(
            [
                "Total",
                "",
                "",
                "",
                "",
                f"{st['total_debits']:.2f}",
                f"{st['total_credits']:.2f}",
                f"{st['closing_balance']:.2f}",
            ]
        )
        fname = f"statement-{st['merchant_id'][:8]}-{st['period_start'][:10]}_{st['period_end'][:10]}.csv"
        return {
            "statement": st,
            "file_name": fname,
            "file_content": buf.getvalue(),
        }


merchant_statement_service = MerchantStatementService()
