"""
Payment Gateway Settlement Service.

Architecture
───────────────────────────────────────────────────────────────────────────────
Bridges the gap between payment capture and actual bank deposit.

Reality: Online payments (Razorpay, Cashfree, PhonePe) are NOT instant cash.
  1. Customer pays → gateway captures → money sits in PG Clearing
  2. Gateway settles (T+1 to T+3) → money hits bank, minus fees
  3. We record settlement + fees as separate journal entries

Accounting flow:
  Payment captured (online):
    DR Payment Gateway Clearing (1006)
    CR Accounts Receivable

  Settlement received:
    DR Bank (1002)
    CR Payment Gateway Clearing (1006)

  Gateway fees:
    DR Gateway Charges (5011)
    DR Tax on Gateway Charges (5012)  — if applicable
    CR Payment Gateway Clearing (1006)

Cash payments bypass this entirely → DR Cash, CR AR (as before).

Usage:
  from app.services.settlement_service import settlement_service
  await settlement_service.record_settlement(...)
───────────────────────────────────────────────────────────────────────────────
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from uuid import UUID

from app.core.database import get_connection, get_serializable_transaction
from app.core.exceptions import ValidationError, ConflictError
from app.core.logging import get_logger
from app.services.accounting_engine import accounting_engine

logger = get_logger(__name__)


def _quantize(val) -> Decimal:
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# Methods that go through payment gateway (not cash)
ONLINE_METHODS = {"upi", "card", "credit_card", "debit_card", "online", "wallet", "bank_transfer", "neft", "rtgs"}


def is_online_payment(method: str) -> bool:
    """Check if a payment method goes through a payment gateway."""
    return (method or "").lower() in ONLINE_METHODS


class SettlementService:
    """Track and reconcile payment gateway settlements."""

    async def record_settlement(
        self,
        *,
        restaurant_id: str,
        gateway: str,
        settlement_id: Optional[str] = None,
        settlement_date: Optional[date] = None,
        gross_amount: float,
        gateway_fee: float = 0,
        tax_on_fee: float = 0,
        net_amount: Optional[float] = None,
        payment_ids: Optional[list[str]] = None,
        branch_id: Optional[str] = None,
        notes: str = "",
        created_by: str = "system",
    ) -> dict:
        """
        Record a gateway settlement batch.

        Creates two journal entries:
          1. DR Bank, CR PG Clearing (net amount deposited)
          2. DR Gateway Charges + Tax, CR PG Clearing (fees deducted)

        Returns settlement record with journal IDs.
        """
        restaurant_uuid = UUID(restaurant_id)
        branch_uuid = UUID(branch_id) if branch_id else None
        s_date = settlement_date or date.today()

        gross = _quantize(gross_amount)
        fee = _quantize(gateway_fee)
        tax = _quantize(tax_on_fee)
        net = _quantize(net_amount) if net_amount is not None else (gross - fee - tax)

        if gross <= 0:
            raise ValidationError("Gross amount must be positive")
        if net < 0:
            raise ValidationError("Net amount cannot be negative")

        payment_uuids = [UUID(pid) for pid in (payment_ids or [])]

        # Idempotency: check if settlement already recorded
        if settlement_id:
            async with get_connection() as conn:
                existing = await conn.fetchrow(
                    "SELECT id, status FROM pg_settlements "
                    "WHERE restaurant_id = $1 AND gateway = $2 AND settlement_id = $3",
                    restaurant_uuid, gateway, settlement_id,
                )
                if existing:
                    return {
                        "settlement_id": str(existing["id"]),
                        "status": existing["status"],
                        "message": "Settlement already recorded",
                    }

        # 1. Journal: DR Bank, CR PG Clearing (net amount)
        settlement_journal_id = None
        if net > 0:
            settlement_ref = f"stl_{gateway}_{settlement_id or s_date.isoformat()}"
            settlement_journal_id = await accounting_engine.create_journal_entry(
                reference_type="settlement",
                reference_id=settlement_ref,
                restaurant_id=restaurant_id,
                branch_id=branch_id,
                description=f"PG settlement — {gateway} {settlement_id or ''} ({s_date})",
                created_by=created_by,
                source_event="PG_SETTLEMENT",
                lines=[
                    {"account": "BANK", "debit": float(net), "credit": 0,
                     "description": f"Bank deposit — {gateway} settlement"},
                    {"account": "PG_CLEARING", "debit": 0, "credit": float(net),
                     "description": f"Clearing settled — {gateway}"},
                ],
            )

        # 2. Journal: DR Gateway Charges + Tax, CR PG Clearing (fees)
        fee_journal_id = None
        total_fees = fee + tax
        if total_fees > 0:
            fee_ref = f"stl_fee_{gateway}_{settlement_id or s_date.isoformat()}"
            fee_lines = []
            if fee > 0:
                fee_lines.append(
                    {"account": "GATEWAY_CHARGES", "debit": float(fee), "credit": 0,
                     "description": f"Gateway fee — {gateway}"}
                )
            if tax > 0:
                fee_lines.append(
                    {"account": "GATEWAY_TAX", "debit": float(tax), "credit": 0,
                     "description": f"GST on gateway fee — {gateway}"}
                )
            fee_lines.append(
                {"account": "PG_CLEARING", "debit": 0, "credit": float(total_fees),
                 "description": f"Fees deducted — {gateway}"}
            )

            fee_journal_id = await accounting_engine.create_journal_entry(
                reference_type="gateway_fee",
                reference_id=fee_ref,
                restaurant_id=restaurant_id,
                branch_id=branch_id,
                description=f"Gateway fees — {gateway} {settlement_id or ''} ({s_date})",
                created_by=created_by,
                source_event="PG_SETTLEMENT",
                lines=fee_lines,
            )

        # 3. Insert settlement record
        async with get_serializable_transaction() as conn:
            row_id = await conn.fetchval(
                """
                INSERT INTO pg_settlements
                    (restaurant_id, branch_id, gateway, settlement_id,
                     settlement_date, gross_amount, gateway_fee, tax_on_fee,
                     net_amount, status, payment_ids,
                     settlement_journal_id, fee_journal_id,
                     notes, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'received',
                        $10, $11, $12, $13, $14)
                RETURNING id
                """,
                restaurant_uuid, branch_uuid, gateway, settlement_id,
                s_date, float(gross), float(fee), float(tax), float(net),
                payment_uuids, settlement_journal_id, fee_journal_id,
                notes, created_by,
            )

        logger.info(
            "pg_settlement_recorded",
            settlement_db_id=str(row_id),
            gateway=gateway,
            gross=float(gross),
            fee=float(fee),
            net=float(net),
        )

        return {
            "settlement_id": str(row_id),
            "gateway": gateway,
            "settlement_date": s_date.isoformat(),
            "gross_amount": float(gross),
            "gateway_fee": float(fee),
            "tax_on_fee": float(tax),
            "net_amount": float(net),
            "status": "received",
            "settlement_journal_id": settlement_journal_id,
            "fee_journal_id": fee_journal_id,
        }

    async def reconcile_settlement(
        self,
        *,
        settlement_db_id: str,
        restaurant_id: str,
        reconciled_by: str,
        notes: str = "",
    ) -> dict:
        """Mark a settlement as reconciled (verified against bank statement)."""
        s_uuid = UUID(settlement_db_id)
        r_uuid = UUID(restaurant_id)

        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                "SELECT id, status FROM pg_settlements "
                "WHERE id = $1 AND restaurant_id = $2 FOR UPDATE",
                s_uuid, r_uuid,
            )
            if not row:
                raise ValidationError("Settlement not found")
            if row["status"] == "reconciled":
                return {"settlement_id": settlement_db_id, "status": "reconciled", "message": "Already reconciled"}

            await conn.execute(
                "UPDATE pg_settlements SET status = 'reconciled', "
                "reconciled_by = $1, reconciled_at = NOW(), notes = $2, updated_at = NOW() "
                "WHERE id = $3",
                reconciled_by, notes, s_uuid,
            )

        return {"settlement_id": settlement_db_id, "status": "reconciled"}

    async def list_settlements(
        self,
        restaurant_id: str,
        *,
        status: Optional[str] = None,
        gateway: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """List settlements with filters."""
        restaurant_uuid = UUID(restaurant_id)

        conditions = ["restaurant_id = $1"]
        params: list = [restaurant_uuid]
        idx = 2

        if status:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1

        if gateway:
            conditions.append(f"gateway = ${idx}")
            params.append(gateway)
            idx += 1

        if from_date:
            conditions.append(f"settlement_date >= ${idx}")
            params.append(from_date)
            idx += 1

        if to_date:
            conditions.append(f"settlement_date <= ${idx}")
            params.append(to_date)
            idx += 1

        where = " AND ".join(conditions)
        params.extend([limit, offset])

        async with get_connection() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, gateway, settlement_id, settlement_date,
                       gross_amount, gateway_fee, tax_on_fee, net_amount,
                       status, payment_ids,
                       settlement_journal_id, fee_journal_id,
                       reconciled_by, reconciled_at, notes,
                       created_by, created_at
                FROM pg_settlements
                WHERE {where}
                ORDER BY settlement_date DESC, created_at DESC
                LIMIT ${idx} OFFSET ${idx + 1}
                """,
                *params,
            )

            count = await conn.fetchval(
                f"SELECT COUNT(*) FROM pg_settlements WHERE {where}",
                *params[:-2],
            )

        return {
            "total": count,
            "settlements": [
                {
                    "id": str(r["id"]),
                    "gateway": r["gateway"],
                    "settlement_id": r["settlement_id"],
                    "settlement_date": r["settlement_date"].isoformat(),
                    "gross_amount": float(r["gross_amount"]),
                    "gateway_fee": float(r["gateway_fee"]),
                    "tax_on_fee": float(r["tax_on_fee"]),
                    "net_amount": float(r["net_amount"]),
                    "status": r["status"],
                    "payment_ids": [str(p) for p in (r["payment_ids"] or [])],
                    "settlement_journal_id": str(r["settlement_journal_id"]) if r["settlement_journal_id"] else None,
                    "fee_journal_id": str(r["fee_journal_id"]) if r["fee_journal_id"] else None,
                    "reconciled_by": r["reconciled_by"],
                    "reconciled_at": r["reconciled_at"].isoformat() if r["reconciled_at"] else None,
                    "notes": r["notes"],
                    "created_by": r["created_by"],
                    "created_at": r["created_at"].isoformat(),
                }
                for r in rows
            ],
            "limit": limit,
            "offset": offset,
        }

    async def get_clearing_balance(self, restaurant_id: str) -> dict:
        """
        Get the current PG Clearing account balance.
        Positive balance = money captured but not yet settled.
        """
        restaurant_uuid = UUID(restaurant_id)

        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COALESCE(SUM(jl.debit), 0) AS total_debit,
                    COALESCE(SUM(jl.credit), 0) AS total_credit
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.journal_entry_id
                JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE je.restaurant_id = $1
                  AND je.is_reversed = false
                  AND coa.system_code = 'PG_CLEARING'
                """,
                restaurant_uuid,
            )

        debit = Decimal(str(row["total_debit"])) if row else Decimal("0")
        credit = Decimal(str(row["total_credit"])) if row else Decimal("0")
        balance = debit - credit  # asset account: debit-normal

        return {
            "clearing_balance": float(balance),
            "description": "Money captured by gateways but not yet settled to bank"
            if balance > 0 else "All settlements received",
        }

    async def get_unsettled_payments(
        self,
        restaurant_id: str,
        *,
        gateway: Optional[str] = None,
        limit: int = 100,
    ) -> dict:
        """
        Find online payments that haven't been included in any settlement yet.
        These are the payments sitting in PG Clearing.
        """
        restaurant_uuid = UUID(restaurant_id)

        async with get_connection() as conn:
            conditions = [
                "p.restaurant_id = $1",
                "p.status = 'completed'",
                "p.payment_method NOT IN ('cash', 'CASH')",
            ]
            params: list = [restaurant_uuid]
            idx = 2

            if gateway:
                # Filter by gateway if payment has a gateway field
                conditions.append(f"COALESCE(p.gateway, 'razorpay') = ${idx}")
                params.append(gateway)
                idx += 1

            params.append(limit)
            where = " AND ".join(conditions)

            rows = await conn.fetch(
                f"""
                SELECT p.id, p.order_id, p.amount, p.payment_method,
                       p.created_at
                FROM payments p
                WHERE {where}
                  AND NOT EXISTS (
                      SELECT 1 FROM pg_settlements s
                      WHERE p.id = ANY(s.payment_ids)
                        AND s.status IN ('received', 'reconciled')
                  )
                ORDER BY p.created_at DESC
                LIMIT ${idx}
                """,
                *params,
            )

        total_unsettled = sum(float(r["amount"]) for r in rows)

        return {
            "unsettled_count": len(rows),
            "total_unsettled": total_unsettled,
            "payments": [
                {
                    "id": str(r["id"]),
                    "order_id": str(r["order_id"]) if r["order_id"] else None,
                    "amount": float(r["amount"]),
                    "method": r["payment_method"],
                    "created_at": r["created_at"].isoformat(),
                }
                for r in rows
            ],
        }


# Singleton
settlement_service = SettlementService()
