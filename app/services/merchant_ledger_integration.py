"""
Merchant Ledger Integration helpers — best-effort, never-raise wrappers
that wire the existing payment / settlement code paths to the immutable
merchant_ledger (Phase 1).

DESIGN CONTRACT
───────────────
1.  Every helper here is **best-effort**.  A ledger failure is LOGGED but
    NEVER re-raised, so it cannot rollback or break the upstream
    payment / settlement transaction.  The existing journal_entries flow
    remains the source of truth; merchant_ledger is an additional,
    parallel record optimised for money-movement queries.

2.  Every helper supplies a deterministic `idempotency_key`, so
    re-deliveries (retries, webhook replays, polling backfills) do NOT
    create duplicate ledger entries.

3.  Posts can run inside or outside an outer transaction.  Pass
    `conn=` to participate; otherwise a fresh transaction is opened.

4.  All helpers no-op when:
      * merchant_id is missing,
      * amount is zero or negative,
      * the underlying service raises (we swallow + log).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from app.core.logging import get_logger
from app.services.merchant_ledger_service import merchant_ledger_service

logger = get_logger(__name__)


def _to_decimal(v) -> Decimal:
    if v is None:
        return Decimal("0")
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal("0")


# ════════════════════════════════════════════════════════════════════════
# PAYMENT RECEIVED
# ════════════════════════════════════════════════════════════════════════
async def post_payment_received(
    *,
    merchant_id: Optional[str],
    payment_id: str,
    amount,
    method: Optional[str] = None,
    order_id: Optional[str] = None,
    branch_id: Optional[str] = None,
    bank_reference: Optional[str] = None,
    utr_number: Optional[str] = None,
    actor_id: Optional[str] = None,
    extra_metadata: Optional[dict[str, Any]] = None,
    conn=None,
) -> None:
    """
    CREDIT the merchant ledger for a captured/completed payment.

    Idempotency: `payment_received:{payment_id}` — calling twice with the
    same payment_id is a no-op (returns the original entry).
    """
    if not merchant_id or not payment_id:
        return
    amt = _to_decimal(amount)
    if amt <= 0:
        return
    metadata = {"method": method, **(extra_metadata or {})}
    try:
        await merchant_ledger_service.post_entry(
            merchant_id=merchant_id,
            branch_id=branch_id,
            transaction_type="payment_received",
            credit_amount=amt,
            currency="INR",
            source_type="payment",
            payment_id=payment_id,
            order_id=order_id,
            bank_reference=bank_reference,
            utr_number=utr_number,
            idempotency_key=f"payment_received:{payment_id}",
            metadata=metadata,
            created_by=actor_id,
            conn=conn,
        )
    except Exception as exc:
        logger.error(
            "merchant_ledger.payment_received.failed",
            extra={
                "payment_id": str(payment_id),
                "merchant_id": str(merchant_id),
                "amount": float(amt),
                "error": str(exc),
            },
        )


# ════════════════════════════════════════════════════════════════════════
# REFUND
# ════════════════════════════════════════════════════════════════════════
async def post_refund(
    *,
    merchant_id: Optional[str],
    payment_id: str,
    amount,
    refund_id: Optional[str] = None,
    order_id: Optional[str] = None,
    branch_id: Optional[str] = None,
    actor_id: Optional[str] = None,
    extra_metadata: Optional[dict[str, Any]] = None,
    conn=None,
) -> None:
    """
    DEBIT the merchant ledger for a refund.

    Idempotency: `refund:{payment_id}:{refund_id or 'full'}` — supports
    multiple partial refunds against one payment.
    """
    if not merchant_id or not payment_id:
        return
    amt = _to_decimal(amount)
    if amt <= 0:
        return
    suffix = refund_id or "full"
    metadata = {"refund_id": refund_id, **(extra_metadata or {})}
    try:
        await merchant_ledger_service.post_entry(
            merchant_id=merchant_id,
            branch_id=branch_id,
            transaction_type="refund",
            debit_amount=amt,
            currency="INR",
            source_type="refund",
            source_id=refund_id,
            payment_id=payment_id,
            order_id=order_id,
            idempotency_key=f"refund:{payment_id}:{suffix}",
            metadata=metadata,
            created_by=actor_id,
            conn=conn,
        )
    except Exception as exc:
        logger.error(
            "merchant_ledger.refund.failed",
            extra={
                "payment_id": str(payment_id),
                "refund_id": str(refund_id) if refund_id else None,
                "amount": float(amt),
                "error": str(exc),
            },
        )


# ════════════════════════════════════════════════════════════════════════
# SETTLEMENT — SETTLED
# ════════════════════════════════════════════════════════════════════════
async def post_settlement_settled(
    *,
    settlement_row: dict,
    actor_id: Optional[str] = None,
    conn=None,
) -> None:
    """
    Mirror a `settled` settlement into the merchant ledger as a sequence of
    immutable debits whose total equals the gross amount of the batch:

        DEBIT settlement_completed   = net_settlement_amount
        DEBIT fee_deduction          = bittu_fee_amount
        DEBIT gst_deduction          = gst_amount
        ────────────────────────────────────────────────
        TOTAL                        = gross_amount

    This precisely offsets the `payment_received` credits posted earlier
    for the underlying payments in the batch.

    Idempotency: per-(settlement_id, kind) — re-running the
    settled→reversed→settled cycle produces ONE entry per kind per
    settlement, not duplicates.
    """
    sid          = settlement_row.get("id")
    merchant_id  = settlement_row.get("restaurant_id")
    branch_id    = settlement_row.get("branch_id")
    if not sid or not merchant_id:
        return

    sid_str = str(sid)
    bank_ref = settlement_row.get("bank_reference_number")
    settlement_ref = settlement_row.get("settlement_reference")

    net = _to_decimal(settlement_row.get("net_settlement_amount"))
    fee = _to_decimal(settlement_row.get("bittu_fee_amount"))
    gst = _to_decimal(settlement_row.get("gst_amount"))

    base_meta = {
        "settlement_reference": settlement_ref,
        "settlement_status": "settled",
    }

    legs = [
        ("settlement_completed", net, "net_settlement_to_bank"),
        ("fee_deduction",         fee, "bittu_platform_fee"),
        ("gst_deduction",         gst, "gst_on_platform_fee"),
    ]
    for txn_type, amt, kind in legs:
        if amt <= 0:
            continue
        try:
            await merchant_ledger_service.post_entry(
                merchant_id=merchant_id,
                branch_id=branch_id,
                transaction_type=txn_type,
                debit_amount=amt,
                currency="INR",
                source_type="settlement",
                settlement_id=sid_str,
                bank_reference=bank_ref,
                idempotency_key=f"settlement_settled:{sid_str}:{kind}",
                metadata={**base_meta, "leg": kind},
                created_by=actor_id,
                conn=conn,
            )
        except Exception as exc:
            logger.error(
                "merchant_ledger.settlement_settled.failed",
                extra={
                    "settlement_id": sid_str,
                    "leg": kind,
                    "amount": float(amt),
                    "error": str(exc),
                },
            )


# ════════════════════════════════════════════════════════════════════════
# SETTLEMENT — REVERSED
# ════════════════════════════════════════════════════════════════════════
async def post_settlement_reversed(
    *,
    settlement_row: dict,
    actor_id: Optional[str] = None,
    conn=None,
) -> None:
    """
    Mirror a `reversed` settlement: post CREDIT entries that exactly undo
    the previously-settled debits.  Idempotent per (settlement_id, kind).
    """
    sid         = settlement_row.get("id")
    merchant_id = settlement_row.get("restaurant_id")
    branch_id   = settlement_row.get("branch_id")
    if not sid or not merchant_id:
        return

    sid_str = str(sid)
    settlement_ref = settlement_row.get("settlement_reference")

    net = _to_decimal(settlement_row.get("net_settlement_amount"))
    fee = _to_decimal(settlement_row.get("bittu_fee_amount"))
    gst = _to_decimal(settlement_row.get("gst_amount"))

    legs = [
        ("settlement_reversed", net, "net_settlement_reversed"),
        ("adjustment",          fee, "fee_reversed"),
        ("adjustment",          gst, "gst_reversed"),
    ]
    base_meta = {
        "settlement_reference": settlement_ref,
        "settlement_status": "reversed",
    }
    for txn_type, amt, kind in legs:
        if amt <= 0:
            continue
        try:
            await merchant_ledger_service.post_entry(
                merchant_id=merchant_id,
                branch_id=branch_id,
                transaction_type=txn_type,
                credit_amount=amt,
                currency="INR",
                source_type="settlement_reversal",
                settlement_id=sid_str,
                idempotency_key=f"settlement_reversed:{sid_str}:{kind}",
                metadata={**base_meta, "leg": kind},
                created_by=actor_id,
                conn=conn,
            )
        except Exception as exc:
            logger.error(
                "merchant_ledger.settlement_reversed.failed",
                extra={
                    "settlement_id": sid_str,
                    "leg": kind,
                    "amount": float(amt),
                    "error": str(exc),
                },
            )
