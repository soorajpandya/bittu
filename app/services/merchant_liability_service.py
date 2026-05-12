"""
Service wrapper around `merchant_liability_ledger` (migration 052).

This is the platform's payable-side accounting:
  * how much do we owe each merchant right now
  * which obligations are aging (settlement_obligation, refund_liability,
    dispute_provision)
  * which reserves are being held against each merchant

Append-only. Adjustments go through the `reversal` kind, never UPDATE.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from app.core.database import get_service_connection
from app.core.logging import get_logger

logger = get_logger(__name__)


VALID_KINDS = frozenset({
    "settlement_obligation", "reserve_hold", "reserve_release",
    "refund_liability", "dispute_provision", "dispute_release",
    "payout_initiated", "payout_failed", "manual_adjustment", "reversal",
})


class MerchantLiabilityService:
    async def post(
        self,
        *,
        merchant_id: str | uuid.UUID,
        liability_kind: str,
        debit: Decimal | int | float = 0,
        credit: Decimal | int | float = 0,
        currency: str = "INR",
        source_type: Optional[str] = None,
        source_id: Optional[str | uuid.UUID] = None,
        metadata: Optional[dict] = None,
        idempotency_key: Optional[str] = None,
        branch_id: Optional[str | uuid.UUID] = None,
        payment_id: Optional[str | uuid.UUID] = None,
        refund_id: Optional[str | uuid.UUID] = None,
        settlement_id: Optional[str | uuid.UUID] = None,
        payout_id: Optional[str | uuid.UUID] = None,
        dispute_id: Optional[str | uuid.UUID] = None,
        due_at: Optional[datetime] = None,
        reversed_entry_id: Optional[str | uuid.UUID] = None,
        reversal_reason: Optional[str] = None,
        created_by: Optional[str | uuid.UUID] = None,
        conn=None,
    ) -> dict:
        if liability_kind not in VALID_KINDS:
            raise ValueError(f"invalid liability_kind: {liability_kind}")
        d = Decimal(str(debit or 0))
        c = Decimal(str(credit or 0))
        if (d > 0) == (c > 0):
            raise ValueError("exactly one of debit/credit must be > 0")

        params = (
            uuid.UUID(str(merchant_id)),
            uuid.UUID(str(branch_id)) if branch_id else None,
            liability_kind,
            d, c,
            currency.upper(),
            source_type,
            uuid.UUID(str(source_id)) if source_id else None,
            json.dumps(metadata or {}, default=str, sort_keys=True),
            idempotency_key,
            uuid.UUID(str(payment_id))    if payment_id    else None,
            uuid.UUID(str(refund_id))     if refund_id     else None,
            uuid.UUID(str(settlement_id)) if settlement_id else None,
            uuid.UUID(str(payout_id))     if payout_id     else None,
            uuid.UUID(str(dispute_id))    if dispute_id    else None,
            due_at,
            uuid.UUID(str(reversed_entry_id)) if reversed_entry_id else None,
            reversal_reason,
            uuid.UUID(str(created_by)) if created_by else None,
        )
        sql = """
            SELECT fn_post_merchant_liability_entry(
                $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10,
                $11, $12, $13, $14, $15, $16, $17, $18, $19
            ) AS result
        """
        try:
            if conn is not None:
                row = await conn.fetchrow(sql, *params)
            else:
                async with get_service_connection() as c2:
                    row = await c2.fetchrow(sql, *params)
        except Exception:
            logger.exception(
                "merchant_liability_post_failed",
                merchant_id=str(merchant_id),
                liability_kind=liability_kind,
                idempotency_key=idempotency_key,
            )
            raise

        result = row["result"]
        if isinstance(result, str):
            result = json.loads(result)
        return result

    async def current_balance(self, merchant_id: str | uuid.UUID) -> Decimal:
        async with get_service_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT balance_after FROM merchant_liability_ledger
                 WHERE merchant_id = $1
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                uuid.UUID(str(merchant_id)),
            )
        return Decimal(str(row["balance_after"])) if row else Decimal("0")

    async def aging(
        self,
        merchant_id: str | uuid.UUID,
        *,
        as_of: Optional[datetime] = None,
    ) -> dict:
        """Bucketed aging of outstanding liabilities (0-7d, 8-30d, 31-60d, 60+d)."""
        async with get_service_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT
                  liability_kind,
                  CASE
                    WHEN due_at IS NULL                       THEN 'undated'
                    WHEN COALESCE($2, NOW()) - due_at <= INTERVAL '7 days'  THEN '0_7d'
                    WHEN COALESCE($2, NOW()) - due_at <= INTERVAL '30 days' THEN '8_30d'
                    WHEN COALESCE($2, NOW()) - due_at <= INTERVAL '60 days' THEN '31_60d'
                    ELSE '60_plus'
                  END AS bucket,
                  SUM(credit_amount - debit_amount) AS net
                FROM merchant_liability_ledger
                WHERE merchant_id = $1
                GROUP BY liability_kind, bucket
                """,
                uuid.UUID(str(merchant_id)), as_of,
            )
        out: dict = {}
        for r in rows:
            out.setdefault(r["liability_kind"], {})[r["bucket"]] = str(r["net"])
        return out


merchant_liability_service = MerchantLiabilityService()
