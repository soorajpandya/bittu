"""
Bank Reconciliation Engine — Phase 3 of Bittu fintech reconciliation core.

Distinct from the legacy ``bank_recon_service`` (which manages the older
``bank_reconciliation`` ledger-line tool).  This service owns the new
``bank_recon_*`` table family added in migration 039 and the platform-admin /
merchant separation.

Pipeline
────────
    CSV upload / webhook
        └─ ingest_csv() / ingest_rows()
              ├─ create import row
              ├─ for each row: compute line_hash, INSERT … ON CONFLICT DO NOTHING
              └─ mark import 'completed' / 'failed'

    Match engine
        └─ run_match_engine(merchant_id=…, scope=…, is_admin_run=…)
              ├─ create run row
              ├─ for each unmatched line:
              │     1. try settlement match  (UTR exact, then amount fuzzy)
              │     2. fallback: escrow_release match (UTR + amount)
              │     3. update line.match_status / matched_*_id
              │     4. emit discrepancy if amount/date variance
              ├─ scan settled bittu_settlements with no bank line in window
              │     → emit 'missing_in_bank' discrepancy
              └─ mark run 'completed', persist summary

All ops are scoped — pass ``merchant_id=None`` (admin only) to run globally.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Optional
from uuid import UUID

from app.core.database import get_connection, get_transaction
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

AMOUNT_TOLERANCE        = Decimal("0.50")
DATE_WINDOW_DAYS        = 2
MISSING_BANK_GRACE_DAYS = 3

DISCREPANCY_KINDS = frozenset({
    "missing_in_bank", "missing_in_settlement", "amount_mismatch",
    "date_mismatch", "duplicate_bank_line", "orphan_credit", "orphan_debit",
})


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────
def _f(v) -> float:
    if v is None:
        return 0.0
    return float(Decimal(str(v)).quantize(Decimal("0.0001")))


def _row_hash(
    *,
    posted_date: date,
    amount: Decimal,
    bank_reference: Optional[str],
    narration: Optional[str],
    counterparty: Optional[str],
) -> str:
    canon = "|".join([
        posted_date.isoformat(),
        f"{Decimal(str(amount)).quantize(Decimal('0.0001'))}",
        (bank_reference or "").strip().upper(),
        (narration or "").strip()[:200],
        (counterparty or "").strip()[:120],
    ])
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _parse_date(s: str) -> date:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y", "%d/%b/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValidationError(f"unrecognised date format: {s!r}")


def _parse_amount(credit: str, debit: str) -> Decimal:
    def _num(x):
        x = (x or "").replace(",", "").replace("\u20b9", "").strip()
        if not x:
            return Decimal("0")
        try:
            return Decimal(x)
        except Exception as e:
            raise ValidationError(f"bad amount {x!r}") from e
    c = _num(credit)
    d = _num(debit)
    if c > 0 and d > 0:
        raise ValidationError("row has both credit and debit amounts")
    if c == 0 and d == 0:
        raise ValidationError("row has neither credit nor debit")
    return c if c > 0 else -d


# ──────────────────────────────────────────────────────────────────────────
# Service
# ──────────────────────────────────────────────────────────────────────────
class ReconEngineService:

    # ── Platform-admin membership ────────────────────────────────────
    async def is_platform_admin(self, user_id: str | UUID) -> bool:
        async with get_connection() as c:
            return bool(await c.fetchval(
                "SELECT fn_is_platform_admin($1::uuid)", str(user_id)
            ))

    async def add_platform_admin(
        self, *, user_id: str | UUID,
        email: Optional[str] = None,
        notes: Optional[str] = None,
        created_by: Optional[str | UUID] = None,
    ) -> dict:
        async with get_transaction() as cx:
            row = await cx.fetchrow(
                """
                INSERT INTO platform_admin_users (user_id, email, notes, created_by)
                VALUES ($1::uuid, $2, $3, $4::uuid)
                ON CONFLICT (user_id) DO UPDATE
                  SET email = EXCLUDED.email, notes = EXCLUDED.notes
                RETURNING *
                """,
                str(user_id), email, notes,
                str(created_by) if created_by else None,
            )
        return {
            "user_id":    str(row["user_id"]),
            "email":      row["email"],
            "notes":      row["notes"],
            "created_at": row["created_at"].isoformat(),
            "created_by": str(row["created_by"]) if row["created_by"] else None,
        }

    async def remove_platform_admin(self, user_id: str | UUID) -> bool:
        async with get_transaction() as cx:
            res = await cx.execute(
                "DELETE FROM platform_admin_users WHERE user_id = $1::uuid",
                str(user_id),
            )
        return res != "DELETE 0"

    async def list_platform_admins(self) -> list[dict]:
        async with get_connection() as c:
            rows = await c.fetch(
                "SELECT * FROM platform_admin_users ORDER BY created_at DESC"
            )
        return [
            {
                "user_id":    str(r["user_id"]),
                "email":      r["email"],
                "notes":      r["notes"],
                "created_at": r["created_at"].isoformat(),
                "created_by": str(r["created_by"]) if r["created_by"] else None,
            }
            for r in rows
        ]

    # ── Bank account registry ────────────────────────────────────────
    async def create_account(
        self,
        *,
        merchant_id: str | UUID,
        account_label: str,
        bank_name: Optional[str] = None,
        account_number_last4: Optional[str] = None,
        ifsc: Optional[str] = None,
        currency: str = "INR",
        metadata: Optional[dict] = None,
    ) -> dict:
        if not account_label or len(account_label) < 2:
            raise ValidationError("account_label is required (min 2 chars)")
        async with get_transaction() as cx:
            row = await cx.fetchrow(
                """
                INSERT INTO bank_recon_accounts
                    (merchant_id, account_label, bank_name,
                     account_number_last4, ifsc, currency, metadata)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::jsonb)
                ON CONFLICT (merchant_id, account_label) DO UPDATE
                  SET bank_name            = EXCLUDED.bank_name,
                      account_number_last4 = EXCLUDED.account_number_last4,
                      ifsc                 = EXCLUDED.ifsc,
                      metadata             = EXCLUDED.metadata,
                      updated_at           = now()
                RETURNING *
                """,
                str(merchant_id), account_label, bank_name,
                account_number_last4, ifsc, (currency or "INR").upper(),
                json.dumps(metadata or {}),
            )
        return self._account_to_dict(row)

    async def deactivate_account(
        self, *, account_id: str | UUID,
        merchant_id: Optional[str | UUID] = None,
    ) -> dict:
        clauses = ["id = $1::uuid"]
        params: list[Any] = [str(account_id)]
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        async with get_transaction() as cx:
            row = await cx.fetchrow(
                f"UPDATE bank_recon_accounts SET is_active=false, updated_at=now() "
                f"WHERE {' AND '.join(clauses)} RETURNING *",
                *params,
            )
        if row is None:
            raise NotFoundError("bank_recon_account", str(account_id))
        return self._account_to_dict(row)

    async def list_accounts(
        self,
        *,
        merchant_id: Optional[str | UUID] = None,
        only_active: bool = True,
    ) -> list[dict]:
        clauses, params = [], []
        if merchant_id:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        if only_active:
            clauses.append("is_active = true")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        async with get_connection() as c:
            rows = await c.fetch(
                f"SELECT * FROM bank_recon_accounts{where} "
                f"ORDER BY merchant_id, account_label",
                *params,
            )
        return [self._account_to_dict(r) for r in rows]

    def _account_to_dict(self, row) -> dict:
        if row is None:
            return {}
        md = row["metadata"]
        if isinstance(md, str):
            md = json.loads(md)
        return {
            "id":                   str(row["id"]),
            "merchant_id":          str(row["merchant_id"]),
            "account_label":        row["account_label"],
            "bank_name":            row["bank_name"],
            "account_number_last4": row["account_number_last4"],
            "ifsc":                 row["ifsc"],
            "currency":             row["currency"],
            "is_active":            bool(row["is_active"]),
            "metadata":             md or {},
            "created_at":           row["created_at"].isoformat(),
            "updated_at":           row["updated_at"].isoformat(),
        }

    async def _assert_account_belongs(
        self, account_id: str | UUID, merchant_id: str | UUID
    ) -> None:
        async with get_connection() as c:
            row = await c.fetchrow(
                "SELECT merchant_id, is_active FROM bank_recon_accounts WHERE id=$1::uuid",
                str(account_id),
            )
        if row is None:
            raise NotFoundError("bank_recon_account", str(account_id))
        if str(row["merchant_id"]) != str(merchant_id):
            raise ValidationError("account does not belong to this merchant")
        if not row["is_active"]:
            raise ValidationError("account is inactive")

    # ── Ingest ───────────────────────────────────────────────────────
    async def ingest_rows(
        self,
        *,
        merchant_id: str | UUID,
        account_id: str | UUID,
        rows: Iterable[dict],
        source: str = "manual",
        original_filename: Optional[str] = None,
        imported_by: Optional[str | UUID] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        if source not in ("csv_upload", "webhook", "manual"):
            raise ValidationError(f"unknown source {source!r}")
        await self._assert_account_belongs(account_id, merchant_id)
        rows_list = list(rows)
        async with get_transaction() as cx:
            imp_id = await cx.fetchval(
                """
                INSERT INTO bank_recon_imports
                    (merchant_id, account_id, source, original_filename,
                     row_count, status, metadata, imported_by)
                VALUES ($1::uuid, $2::uuid, $3::bank_recon_import_source,
                        $4, $5, 'processing', $6::jsonb, $7::uuid)
                RETURNING id
                """,
                str(merchant_id), str(account_id), source, original_filename,
                len(rows_list), json.dumps(metadata or {}),
                str(imported_by) if imported_by else None,
            )
            inserted = skipped = 0
            for raw in rows_list:
                try:
                    pd = raw["posted_date"]
                    if isinstance(pd, str):
                        pd = _parse_date(pd)
                    vd = raw.get("value_date")
                    if isinstance(vd, str) and vd:
                        vd = _parse_date(vd)
                    elif not vd:
                        vd = None
                    amt = raw.get("amount")
                    if amt is None:
                        amt = _parse_amount(
                            str(raw.get("credit", "") or ""),
                            str(raw.get("debit",  "") or ""),
                        )
                    else:
                        amt = Decimal(str(amt))
                    if amt == 0:
                        skipped += 1
                        continue
                    bank_ref = (raw.get("bank_reference") or "").strip() or None
                    narr     = (raw.get("narration") or "").strip() or None
                    cp       = (raw.get("counterparty") or "").strip() or None
                    bal_after = raw.get("balance_after")
                    if bal_after not in (None, ""):
                        bal_after = Decimal(str(bal_after))
                    else:
                        bal_after = None
                    h = _row_hash(
                        posted_date=pd, amount=amt,
                        bank_reference=bank_ref, narration=narr, counterparty=cp,
                    )
                    res = await cx.execute(
                        """
                        INSERT INTO bank_recon_lines
                            (import_id, merchant_id, account_id, posted_date, value_date,
                             amount, currency, narration, bank_reference, counterparty,
                             balance_after, line_hash, raw_row)
                        VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6,
                                $7, $8, $9, $10, $11, $12, $13::jsonb)
                        ON CONFLICT (merchant_id, account_id, line_hash) DO NOTHING
                        """,
                        imp_id, str(merchant_id), str(account_id), pd, vd,
                        amt, (raw.get("currency") or "INR").upper(),
                        narr, bank_ref, cp, bal_after, h,
                        json.dumps(raw.get("raw") or raw, default=str),
                    )
                    if res.endswith("0"):
                        skipped += 1
                    else:
                        inserted += 1
                except Exception as exc:
                    skipped += 1
                    logger.warning(
                        "recon_engine.ingest.row_failed",
                        extra={"import_id": str(imp_id), "error": str(exc)},
                    )
            await cx.execute(
                """
                UPDATE bank_recon_imports
                   SET status='completed', completed_at=now(),
                       rows_inserted=$2, rows_skipped=$3
                 WHERE id=$1::uuid
                """,
                imp_id, inserted, skipped,
            )
        logger.info(
            "recon_engine.ingest.done",
            extra={"import_id": str(imp_id), "inserted": inserted, "skipped": skipped},
        )
        return {
            "import_id": str(imp_id),
            "row_count": len(rows_list),
            "inserted":  inserted,
            "skipped":   skipped,
        }

    async def ingest_csv(
        self,
        *,
        merchant_id: str | UUID,
        account_id: str | UUID,
        csv_text: str,
        original_filename: Optional[str] = None,
        imported_by: Optional[str | UUID] = None,
        column_map: Optional[dict[str, str]] = None,
    ) -> dict:
        rdr = csv.DictReader(io.StringIO(csv_text))
        canon_rows: list[dict] = []
        for raw in rdr:
            if not raw:
                continue
            if column_map:
                mapped = {column_map.get(k, k): v for k, v in raw.items()}
            else:
                mapped = {(k or "").strip().lower().replace(" ", "_"): v for k, v in raw.items()}
            mapped["raw"] = dict(raw)
            canon_rows.append(mapped)
        return await self.ingest_rows(
            merchant_id=merchant_id, account_id=account_id, rows=canon_rows,
            source="csv_upload", original_filename=original_filename,
            imported_by=imported_by,
        )

    # ── Match engine ─────────────────────────────────────────────────
    async def run_match_engine(
        self,
        *,
        merchant_id: Optional[str | UUID] = None,
        account_id: Optional[str | UUID] = None,
        scope_from: Optional[date] = None,
        scope_to: Optional[date] = None,
        triggered_by: Optional[str | UUID] = None,
        is_admin_run: bool = False,
    ) -> dict:
        async with get_transaction() as cx:
            run_id = await cx.fetchval(
                """
                INSERT INTO bank_recon_runs
                    (merchant_id, account_id, scope_from, scope_to,
                     triggered_by, is_admin_run, status)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5::uuid, $6, 'running')
                RETURNING id
                """,
                str(merchant_id) if merchant_id else None,
                str(account_id) if account_id else None,
                scope_from, scope_to,
                str(triggered_by) if triggered_by else None,
                is_admin_run,
            )

        summary = {
            "lines_scanned":         0,
            "matched_settlement":    0,
            "matched_escrow":        0,
            "amount_mismatch":       0,
            "still_unmatched":       0,
            "discrepancies_created": 0,
        }
        try:
            clauses = ["match_status = 'unmatched'"]
            params: list[Any] = []
            if merchant_id:
                params.append(str(merchant_id))
                clauses.append(f"merchant_id = ${len(params)}::uuid")
            if account_id:
                params.append(str(account_id))
                clauses.append(f"account_id = ${len(params)}::uuid")
            if scope_from:
                params.append(scope_from)
                clauses.append(f"posted_date >= ${len(params)}")
            if scope_to:
                params.append(scope_to)
                clauses.append(f"posted_date <= ${len(params)}")
            sql = (f"SELECT * FROM bank_recon_lines WHERE {' AND '.join(clauses)} "
                   f"ORDER BY posted_date ASC, created_at ASC LIMIT 5000")
            async with get_connection() as c:
                lines = await c.fetch(sql, *params)
            summary["lines_scanned"] = len(lines)

            for ln in lines:
                if ln["amount"] <= 0:
                    continue  # debits are not auto-matched in Phase 3
                matched = await self._try_match_settlement(ln, run_id)
                if matched:
                    summary["matched_settlement"] += 1
                    if matched.get("amount_mismatch"):
                        summary["amount_mismatch"] += 1
                        summary["discrepancies_created"] += 1
                    continue
                matched = await self._try_match_escrow_release(ln, run_id)
                if matched:
                    summary["matched_escrow"] += 1
                    continue
                summary["still_unmatched"] += 1
                created = await self._emit_discrepancy(
                    run_id=run_id, merchant_id=ln["merchant_id"],
                    account_id=ln["account_id"], kind="orphan_credit",
                    line_id=ln["id"], expected=None, actual=ln["amount"],
                    notes=(ln["narration"] or "")[:500], severity="medium",
                )
                if created:
                    summary["discrepancies_created"] += 1

            unbanked = await self._find_unbanked_settlements(
                merchant_id=merchant_id, scope_from=scope_from, scope_to=scope_to,
            )
            for s in unbanked:
                created = await self._emit_discrepancy(
                    run_id=run_id, merchant_id=s["restaurant_id"],
                    account_id=None, kind="missing_in_bank",
                    settlement_id=s["id"],
                    expected=s["net_settlement_amount"], actual=None,
                    variance=s["net_settlement_amount"],
                    notes=(f"settlement {s['settlement_reference']} settled "
                           f"{s['settled_at'].date() if s['settled_at'] else 'NA'} "
                           f"but no bank line within {DATE_WINDOW_DAYS}d"),
                    severity="high",
                )
                if created:
                    summary["discrepancies_created"] += 1

            async with get_transaction() as cx:
                await cx.execute(
                    "UPDATE bank_recon_runs SET status='completed', "
                    "completed_at=now(), summary=$2::jsonb WHERE id=$1::uuid",
                    run_id, json.dumps(summary),
                )
        except Exception as exc:
            logger.exception("recon_engine.run.failed", extra={"run_id": str(run_id)})
            async with get_transaction() as cx:
                await cx.execute(
                    "UPDATE bank_recon_runs SET status='failed', "
                    "completed_at=now(), error_message=$2 WHERE id=$1::uuid",
                    run_id, str(exc),
                )
            raise
        return {"run_id": str(run_id), "summary": summary}

    async def _try_match_settlement(self, line, run_id) -> Optional[dict]:
        date_lo = line["posted_date"] - timedelta(days=DATE_WINDOW_DAYS)
        date_hi = line["posted_date"] + timedelta(days=DATE_WINDOW_DAYS)
        amount  = line["amount"]
        ref     = (line["bank_reference"] or "").strip()

        chosen = None
        confidence = Decimal("0")

        async with get_connection() as c:
            if ref:
                chosen = await c.fetchrow(
                    """
                    SELECT id, net_settlement_amount, settled_at, restaurant_id
                      FROM bittu_settlements
                     WHERE restaurant_id = $1::uuid
                       AND settlement_status IN ('settled', 'sent_to_bank')
                       AND bank_reference_number = $2
                       AND (settled_at IS NULL OR settled_at::date BETWEEN $3 AND $4)
                       AND id NOT IN (
                            SELECT matched_settlement_id FROM bank_recon_lines
                             WHERE matched_settlement_id IS NOT NULL
                       )
                     LIMIT 1
                    """,
                    str(line["merchant_id"]), ref, date_lo, date_hi,
                )
                if chosen is not None:
                    confidence = Decimal("0.99")

            if chosen is None:
                rows = await c.fetch(
                    """
                    SELECT id, net_settlement_amount, settled_at, restaurant_id
                      FROM bittu_settlements
                     WHERE restaurant_id = $1::uuid
                       AND settlement_status IN ('settled', 'sent_to_bank')
                       AND ABS(net_settlement_amount - $2) <= $3
                       AND (settled_at IS NULL OR settled_at::date BETWEEN $4 AND $5)
                       AND id NOT IN (
                            SELECT matched_settlement_id FROM bank_recon_lines
                             WHERE matched_settlement_id IS NOT NULL
                       )
                     LIMIT 2
                    """,
                    str(line["merchant_id"]), amount, AMOUNT_TOLERANCE,
                    date_lo, date_hi,
                )
                if len(rows) == 1:
                    chosen = rows[0]
                    confidence = Decimal("0.80")

        if chosen is None:
            return None

        diff = (Decimal(str(chosen["net_settlement_amount"])) - Decimal(str(amount))).copy_abs()
        amount_mismatch = diff > Decimal("0.05")

        async with get_transaction() as cx:
            await cx.execute(
                """
                UPDATE bank_recon_lines
                   SET match_status          = $2::bank_recon_line_status,
                       matched_settlement_id = $3::uuid,
                       match_confidence      = $4,
                       matched_at            = now(),
                       matched_by            = 'auto'
                 WHERE id = $1::uuid
                """,
                line["id"],
                "partial" if amount_mismatch else "matched",
                str(chosen["id"]),
                confidence,
            )
            if amount_mismatch:
                await self._emit_discrepancy(
                    run_id=run_id, merchant_id=line["merchant_id"],
                    account_id=line["account_id"], kind="amount_mismatch",
                    line_id=line["id"], settlement_id=chosen["id"],
                    expected=chosen["net_settlement_amount"], actual=amount,
                    variance=diff, severity="high",
                    notes=f"settlement net={chosen['net_settlement_amount']}, bank={amount}",
                    cx=cx,
                )
        return {"settlement_id": str(chosen["id"]), "amount_mismatch": amount_mismatch}

    async def _try_match_escrow_release(self, line, run_id) -> Optional[dict]:
        ref     = (line["bank_reference"] or "").strip()
        amount  = line["amount"]
        if not ref:
            return None
        async with get_connection() as c:
            row = await c.fetchrow(
                """
                SELECT id, debit_amount, merchant_id
                  FROM escrow_ledger
                 WHERE merchant_id      = $1::uuid
                   AND transaction_type = 'escrow_release'
                   AND bank_reference   = $2
                   AND ABS(debit_amount - $3) <= $4
                   AND id NOT IN (
                        SELECT matched_escrow_entry_id FROM bank_recon_lines
                         WHERE matched_escrow_entry_id IS NOT NULL
                   )
                 LIMIT 2
                """,
                str(line["merchant_id"]), ref, amount, AMOUNT_TOLERANCE,
            )
        if row is None:
            return None
        async with get_transaction() as cx:
            await cx.execute(
                """
                UPDATE bank_recon_lines
                   SET match_status='matched', matched_escrow_entry_id=$2::uuid,
                       match_confidence=0.85, matched_at=now(), matched_by='auto'
                 WHERE id=$1::uuid
                """,
                line["id"], str(row["id"]),
            )
        return {"escrow_entry_id": str(row["id"])}

    async def _find_unbanked_settlements(
        self,
        *,
        merchant_id: Optional[str | UUID],
        scope_from: Optional[date],
        scope_to: Optional[date],
    ) -> list[dict]:
        clauses = [
            "s.settlement_status IN ('settled', 'sent_to_bank')",
            "s.settled_at IS NOT NULL",
            "s.settled_at <= now() - $1::interval",
        ]
        params: list[Any] = [timedelta(days=MISSING_BANK_GRACE_DAYS)]
        if merchant_id:
            params.append(str(merchant_id))
            clauses.append(f"s.restaurant_id = ${len(params)}::uuid")
        if scope_from:
            params.append(scope_from)
            clauses.append(f"s.settled_at::date >= ${len(params)}")
        if scope_to:
            params.append(scope_to)
            clauses.append(f"s.settled_at::date <= ${len(params)}")
        sql = f"""
            SELECT s.id, s.restaurant_id, s.settlement_reference,
                   s.net_settlement_amount, s.settled_at
              FROM bittu_settlements s
              LEFT JOIN bank_recon_lines bl
                     ON bl.matched_settlement_id = s.id
             WHERE {' AND '.join(clauses)}
               AND bl.id IS NULL
             ORDER BY s.settled_at ASC
             LIMIT 1000
        """
        async with get_connection() as c:
            return [dict(r) for r in await c.fetch(sql, *params)]

    # ── Discrepancy emit ─────────────────────────────────────────────
    async def _emit_discrepancy(
        self,
        *,
        run_id,
        merchant_id,
        account_id,
        kind: str,
        line_id=None,
        settlement_id=None,
        escrow_entry_id=None,
        expected=None,
        actual=None,
        variance=None,
        notes: Optional[str] = None,
        severity: str = "medium",
        cx=None,
    ) -> bool:
        if kind not in DISCREPANCY_KINDS:
            raise ValidationError(f"unknown discrepancy kind {kind!r}")
        if variance is None and expected is not None and actual is not None:
            variance = Decimal(str(expected)) - Decimal(str(actual))
        sql = """
            INSERT INTO bank_recon_discrepancies
                (run_id, merchant_id, account_id, kind, severity,
                 line_id, settlement_id, escrow_entry_id,
                 expected_amount, actual_amount, variance_amount, notes)
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4::bank_recon_discrepancy_kind,
                    $5, $6::uuid, $7::uuid, $8::uuid, $9, $10, $11, $12)
            ON CONFLICT (merchant_id, kind, line_id, settlement_id, escrow_entry_id)
              DO NOTHING
            RETURNING id
        """
        params = (
            str(run_id), str(merchant_id),
            str(account_id) if account_id else None,
            kind, severity,
            str(line_id) if line_id else None,
            str(settlement_id) if settlement_id else None,
            str(escrow_entry_id) if escrow_entry_id else None,
            expected, actual, variance, notes,
        )
        if cx is not None:
            res = await cx.fetchval(sql, *params)
        else:
            async with get_transaction() as c:
                res = await c.fetchval(sql, *params)
        return res is not None

    # ── Manual match / unmatch ───────────────────────────────────────
    async def manual_match(
        self,
        *,
        line_id: str | UUID,
        actor_id: str | UUID,
        merchant_id: Optional[str | UUID] = None,
        settlement_id: Optional[str | UUID] = None,
        escrow_entry_id: Optional[str | UUID] = None,
    ) -> dict:
        if not (settlement_id or escrow_entry_id):
            raise ValidationError("settlement_id or escrow_entry_id required")
        if settlement_id and escrow_entry_id:
            raise ValidationError("provide only one of settlement_id / escrow_entry_id")
        clauses = ["id = $1::uuid"]
        params: list[Any] = [str(line_id)]
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        async with get_transaction() as cx:
            row = await cx.fetchrow(
                f"""
                UPDATE bank_recon_lines
                   SET match_status            = 'matched',
                       matched_settlement_id   = $%d::uuid,
                       matched_escrow_entry_id = $%d::uuid,
                       match_confidence        = 1.0,
                       matched_at              = now(),
                       matched_by              = $%d
                 WHERE {' AND '.join(clauses)}
                 RETURNING *
                """ % (len(params) + 1, len(params) + 2, len(params) + 3),
                *params,
                str(settlement_id) if settlement_id else None,
                str(escrow_entry_id) if escrow_entry_id else None,
                str(actor_id),
            )
        if row is None:
            raise NotFoundError("bank_recon_line", str(line_id))
        return self._line_to_dict(row)

    async def unmatch_line(
        self, *, line_id: str | UUID,
        actor_id: str | UUID,
        merchant_id: Optional[str | UUID] = None,
    ) -> dict:
        clauses = ["id = $1::uuid"]
        params: list[Any] = [str(line_id)]
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        async with get_transaction() as cx:
            row = await cx.fetchrow(
                f"""
                UPDATE bank_recon_lines
                   SET match_status            = 'unmatched',
                       matched_settlement_id   = NULL,
                       matched_escrow_entry_id = NULL,
                       match_confidence        = NULL,
                       matched_at              = NULL,
                       matched_by              = $%d
                 WHERE {' AND '.join(clauses)}
                 RETURNING *
                """ % (len(params) + 1),
                *params, str(actor_id),
            )
        if row is None:
            raise NotFoundError("bank_recon_line", str(line_id))
        return self._line_to_dict(row)

    # ── Read APIs ────────────────────────────────────────────────────
    async def list_lines(
        self,
        *,
        merchant_id: Optional[str | UUID] = None,
        account_id:  Optional[str | UUID] = None,
        match_status: Optional[str] = None,
        from_date:   Optional[date] = None,
        to_date:     Optional[date] = None,
        limit:       int = 50,
        cursor:      Optional[str] = None,
    ) -> dict:
        limit = max(1, min(int(limit), 200))
        clauses, params = [], []
        if merchant_id:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        if account_id:
            params.append(str(account_id))
            clauses.append(f"account_id = ${len(params)}::uuid")
        if match_status:
            params.append(match_status)
            clauses.append(f"match_status = ${len(params)}::bank_recon_line_status")
        if from_date:
            params.append(from_date)
            clauses.append(f"posted_date >= ${len(params)}")
        if to_date:
            params.append(to_date)
            clauses.append(f"posted_date <= ${len(params)}")
        if cursor:
            try:
                ts, cid = cursor.split("|", 1)
            except ValueError as e:
                raise ValidationError("invalid cursor") from e
            params.append(ts); params.append(cid)
            clauses.append(
                f"(created_at, id) < (${len(params)-1}::timestamptz, ${len(params)}::uuid)"
            )
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit + 1)
        sql = (f"SELECT * FROM bank_recon_lines{where} "
               f"ORDER BY created_at DESC, id DESC LIMIT ${len(params)}")
        async with get_connection() as c:
            rows = await c.fetch(sql, *params)
        items = [self._line_to_dict(r) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = f"{last['created_at'].isoformat()}|{last['id']}"
        return {"items": items, "next_cursor": next_cursor, "count": len(items)}

    async def get_line(
        self, *, line_id: str | UUID,
        merchant_id: Optional[str | UUID] = None,
    ) -> dict:
        clauses = ["id = $1::uuid"]
        params: list[Any] = [str(line_id)]
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        async with get_connection() as c:
            row = await c.fetchrow(
                f"SELECT * FROM bank_recon_lines WHERE {' AND '.join(clauses)}",
                *params,
            )
        if row is None:
            raise NotFoundError("bank_recon_line", str(line_id))
        return self._line_to_dict(row)

    def _line_to_dict(self, row) -> dict:
        raw = row["raw_row"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        return {
            "id":                      str(row["id"]),
            "import_id":               str(row["import_id"]),
            "merchant_id":             str(row["merchant_id"]),
            "account_id":              str(row["account_id"]),
            "posted_date":             row["posted_date"].isoformat(),
            "value_date":              row["value_date"].isoformat() if row["value_date"] else None,
            "amount":                  _f(row["amount"]),
            "currency":                row["currency"],
            "narration":               row["narration"],
            "bank_reference":          row["bank_reference"],
            "counterparty":            row["counterparty"],
            "balance_after":           _f(row["balance_after"]) if row["balance_after"] is not None else None,
            "match_status":            row["match_status"],
            "matched_settlement_id":   str(row["matched_settlement_id"]) if row["matched_settlement_id"] else None,
            "matched_escrow_entry_id": str(row["matched_escrow_entry_id"]) if row["matched_escrow_entry_id"] else None,
            "match_confidence":        float(row["match_confidence"]) if row["match_confidence"] is not None else None,
            "matched_at":              row["matched_at"].isoformat() if row["matched_at"] else None,
            "matched_by":              row["matched_by"],
            "raw_row":                 raw or {},
            "created_at":              row["created_at"].isoformat(),
        }

    async def list_imports(
        self,
        *,
        merchant_id: Optional[str | UUID] = None,
        account_id:  Optional[str | UUID] = None,
        limit: int = 50,
    ) -> list[dict]:
        clauses, params = [], []
        if merchant_id:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        if account_id:
            params.append(str(account_id))
            clauses.append(f"account_id = ${len(params)}::uuid")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(min(int(limit), 200))
        sql = (f"SELECT * FROM bank_recon_imports{where} "
               f"ORDER BY started_at DESC LIMIT ${len(params)}")
        async with get_connection() as c:
            rows = await c.fetch(sql, *params)
        return [
            {
                "id":                str(r["id"]),
                "merchant_id":       str(r["merchant_id"]),
                "account_id":        str(r["account_id"]),
                "source":            r["source"],
                "original_filename": r["original_filename"],
                "row_count":         r["row_count"],
                "rows_inserted":     r["rows_inserted"],
                "rows_skipped":      r["rows_skipped"],
                "status":            r["status"],
                "error_message":     r["error_message"],
                "started_at":        r["started_at"].isoformat(),
                "completed_at":      r["completed_at"].isoformat() if r["completed_at"] else None,
                "imported_by":       str(r["imported_by"]) if r["imported_by"] else None,
            }
            for r in rows
        ]

    async def list_runs(
        self,
        *,
        merchant_id: Optional[str | UUID] = None,
        is_admin_run: Optional[bool] = None,
        limit: int = 50,
    ) -> list[dict]:
        clauses, params = [], []
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        if is_admin_run is not None:
            params.append(is_admin_run)
            clauses.append(f"is_admin_run = ${len(params)}")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(min(int(limit), 200))
        sql = (f"SELECT * FROM bank_recon_runs{where} "
               f"ORDER BY started_at DESC LIMIT ${len(params)}")
        async with get_connection() as c:
            rows = await c.fetch(sql, *params)
        out = []
        for r in rows:
            summ = r["summary"]
            if isinstance(summ, str):
                summ = json.loads(summ)
            out.append({
                "id":            str(r["id"]),
                "merchant_id":   str(r["merchant_id"]) if r["merchant_id"] else None,
                "account_id":    str(r["account_id"])  if r["account_id"]  else None,
                "scope_from":    r["scope_from"].isoformat() if r["scope_from"] else None,
                "scope_to":      r["scope_to"].isoformat()   if r["scope_to"]   else None,
                "is_admin_run":  bool(r["is_admin_run"]),
                "status":        r["status"],
                "summary":       summ or {},
                "error_message": r["error_message"],
                "started_at":    r["started_at"].isoformat(),
                "completed_at":  r["completed_at"].isoformat() if r["completed_at"] else None,
                "triggered_by":  str(r["triggered_by"]) if r["triggered_by"] else None,
            })
        return out

    async def list_discrepancies(
        self,
        *,
        merchant_id: Optional[str | UUID] = None,
        kind:        Optional[str] = None,
        status:      Optional[str] = None,
        severity:    Optional[str] = None,
        from_date:   Optional[date] = None,
        to_date:     Optional[date] = None,
        limit:       int = 50,
        cursor:      Optional[str] = None,
    ) -> dict:
        limit = max(1, min(int(limit), 200))
        clauses, params = [], []
        if merchant_id:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        if kind:
            params.append(kind)
            clauses.append(f"kind = ${len(params)}::bank_recon_discrepancy_kind")
        if status:
            params.append(status)
            clauses.append(f"status = ${len(params)}::bank_recon_discrepancy_status")
        if severity:
            params.append(severity)
            clauses.append(f"severity = ${len(params)}")
        if from_date:
            params.append(from_date)
            clauses.append(f"detected_at::date >= ${len(params)}")
        if to_date:
            params.append(to_date)
            clauses.append(f"detected_at::date <= ${len(params)}")
        if cursor:
            try:
                ts, cid = cursor.split("|", 1)
            except ValueError as e:
                raise ValidationError("invalid cursor") from e
            params.append(ts); params.append(cid)
            clauses.append(
                f"(detected_at, id) < (${len(params)-1}::timestamptz, ${len(params)}::uuid)"
            )
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit + 1)
        sql = (f"SELECT * FROM bank_recon_discrepancies{where} "
               f"ORDER BY detected_at DESC, id DESC LIMIT ${len(params)}")
        async with get_connection() as c:
            rows = await c.fetch(sql, *params)
        items = [self._disc_to_dict(r) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = f"{last['detected_at'].isoformat()}|{last['id']}"
        return {"items": items, "next_cursor": next_cursor, "count": len(items)}

    def _disc_to_dict(self, r) -> dict:
        md = r["metadata"]
        if isinstance(md, str):
            md = json.loads(md)
        return {
            "id":                str(r["id"]),
            "run_id":            str(r["run_id"]) if r["run_id"] else None,
            "merchant_id":       str(r["merchant_id"]),
            "account_id":        str(r["account_id"]) if r["account_id"] else None,
            "kind":              r["kind"],
            "severity":          r["severity"],
            "line_id":           str(r["line_id"]) if r["line_id"] else None,
            "settlement_id":     str(r["settlement_id"]) if r["settlement_id"] else None,
            "escrow_entry_id":   str(r["escrow_entry_id"]) if r["escrow_entry_id"] else None,
            "expected_amount":   _f(r["expected_amount"]) if r["expected_amount"] is not None else None,
            "actual_amount":     _f(r["actual_amount"])   if r["actual_amount"]   is not None else None,
            "variance_amount":   _f(r["variance_amount"]) if r["variance_amount"] is not None else None,
            "notes":             r["notes"],
            "status":            r["status"],
            "resolution_notes":  r["resolution_notes"],
            "resolved_at":       r["resolved_at"].isoformat() if r["resolved_at"] else None,
            "resolved_by":       str(r["resolved_by"]) if r["resolved_by"] else None,
            "metadata":          md or {},
            "detected_at":       r["detected_at"].isoformat(),
        }

    async def resolve_discrepancy(
        self,
        *,
        discrepancy_id: str | UUID,
        actor_id: str | UUID,
        new_status: str,
        merchant_id: Optional[str | UUID] = None,
        resolution_notes: Optional[str] = None,
    ) -> dict:
        if new_status not in ("open", "investigating", "resolved", "ignored"):
            raise ValidationError(f"invalid status {new_status!r}")
        clauses = ["id = $1::uuid"]
        params: list[Any] = [str(discrepancy_id)]
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        # status param index (after the WHERE params)
        s_idx  = len(params) + 1
        rn_idx = len(params) + 2
        ab_idx = len(params) + 3
        async with get_transaction() as cx:
            row = await cx.fetchrow(
                f"""
                UPDATE bank_recon_discrepancies
                   SET status           = ${s_idx}::bank_recon_discrepancy_status,
                       resolution_notes = ${rn_idx},
                       resolved_at      = CASE WHEN ${s_idx}::bank_recon_discrepancy_status
                                                     IN ('resolved','ignored')
                                               THEN now() ELSE NULL END,
                       resolved_by      = ${ab_idx}::uuid
                 WHERE {' AND '.join(clauses)}
                 RETURNING *
                """,
                *params, new_status, resolution_notes, str(actor_id),
            )
        if row is None:
            raise NotFoundError("bank_recon_discrepancy", str(discrepancy_id))
        return self._disc_to_dict(row)

    # ── Aggregates ───────────────────────────────────────────────────
    async def get_summary(
        self, *, merchant_id: Optional[str | UUID] = None,
    ) -> dict:
        clause = ""
        params: list[Any] = []
        if merchant_id is not None:
            params.append(str(merchant_id))
            clause = f"WHERE merchant_id = ${len(params)}::uuid"
        async with get_connection() as c:
            line_summary = await c.fetchrow(
                f"""
                SELECT count(*)                                           AS total,
                       count(*) FILTER (WHERE match_status = 'matched')   AS matched,
                       count(*) FILTER (WHERE match_status = 'partial')   AS partial,
                       count(*) FILTER (WHERE match_status = 'unmatched') AS unmatched,
                       count(*) FILTER (WHERE match_status = 'ignored')   AS ignored,
                       coalesce(sum(amount) FILTER (WHERE amount > 0), 0) AS total_credits,
                       coalesce(sum(amount) FILTER (WHERE amount < 0), 0) AS total_debits
                  FROM bank_recon_lines {clause}
                """,
                *params,
            )
            disc_summary = await c.fetch(
                f"""
                SELECT kind::text AS kind, status::text AS status,
                       severity, count(*) AS cnt
                  FROM bank_recon_discrepancies {clause}
                 GROUP BY kind, status, severity
                """,
                *params,
            )
        return {
            "lines": {
                "total":         int(line_summary["total"] or 0),
                "matched":       int(line_summary["matched"] or 0),
                "partial":       int(line_summary["partial"] or 0),
                "unmatched":     int(line_summary["unmatched"] or 0),
                "ignored":       int(line_summary["ignored"] or 0),
                "total_credits": _f(line_summary["total_credits"]),
                "total_debits":  _f(line_summary["total_debits"]),
            },
            "discrepancies": [
                {"kind": r["kind"], "status": r["status"], "severity": r["severity"],
                 "count": int(r["cnt"])}
                for r in disc_summary
            ],
        }

    async def admin_summary_by_merchant(self) -> list[dict]:
        async with get_connection() as c:
            rows = await c.fetch(
                """
                SELECT merchant_id::text AS merchant_id,
                       count(*)                                            AS total,
                       count(*) FILTER (WHERE match_status = 'matched')    AS matched,
                       count(*) FILTER (WHERE match_status = 'unmatched')  AS unmatched,
                       count(*) FILTER (WHERE match_status = 'partial')    AS partial
                  FROM bank_recon_lines
                 GROUP BY merchant_id
                 ORDER BY count(*) DESC
                """
            )
            disc = await c.fetch(
                """
                SELECT merchant_id::text AS merchant_id,
                       count(*) FILTER (WHERE status IN ('open','investigating'))
                            AS open_disc
                  FROM bank_recon_discrepancies
                 GROUP BY merchant_id
                """
            )
        disc_map = {d["merchant_id"]: int(d["open_disc"]) for d in disc}
        return [
            {
                "merchant_id":          r["merchant_id"],
                "total":                int(r["total"]),
                "matched":              int(r["matched"]),
                "partial":              int(r["partial"]),
                "unmatched":            int(r["unmatched"]),
                "open_discrepancies":   disc_map.get(r["merchant_id"], 0),
            }
            for r in rows
        ]


recon_engine_service = ReconEngineService()
