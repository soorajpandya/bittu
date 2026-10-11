"""Single posting path from Razorpay events into the operational merchant ledger.

The merchant ledger is an *entitlement* ledger that can reach zero:

    payment.captured      +gross (payment_received)
                          -fee   (fee_deduction)   = taxable platform fee
                          -gst   (gst_deduction)   = 18% GST on that fee
                          -> balance == frozen merchant entitlement (95%)
    settlement.processed  -merchant settled paise (settlement_completed)
                          +recovered paise        (settlement_reversed, when a
                                                   settlement line nets negative)
    refund succeeded      -gross refund (refund)   posted by refund_service
                          +fee/gst share back      (fee_reversal / gst_reversal)
    dispute lost          -gross (chargeback)      posted by dispute_service
                          +fee/gst share back      (fee_reversal / gst_reversal)

Fee legs are derived only from the frozen Route allocation
(rzp_route_allocations.merchant_paise); history is never repriced. Cash and
direct POS receipts carry no deduction. No escrow hold is created here:
Razorpay Route pays the merchant's 95% directly, Bittu never custodies it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from app.core.database import get_service_connection, get_transaction
from app.core.logging import get_logger
from app.services import merchant_ledger_integration
from app.services.merchant_ledger_service import merchant_ledger_service
from app.services.razorpay.fee_policy import (
    classify_method,
    gst_split_paise,
    proportional_share_paise,
    rupees_to_paise,
)

logger = get_logger(__name__)


def _rupees(paise: int) -> Decimal:
    return Decimal(int(paise)) / 100


async def _post(conn, **kwargs) -> Optional[dict]:
    return await merchant_ledger_service.post_entry(conn=conn, **kwargs)


async def post_fee_legs(
    *,
    conn,
    merchant_id,
    payment_id,
    gross_paise: int,
    merchant_paise: int,
    branch_id=None,
    order_id=None,
    razorpay_payment_id: Optional[str] = None,
    created_by=None,
) -> dict:
    """Debit the taxable fee and its GST for one captured gateway payment."""
    deduction = int(gross_paise) - int(merchant_paise)
    if deduction < 0:
        raise ValueError("merchant entitlement exceeds gross")
    fee, gst = gst_split_paise(deduction)
    meta = {
        "razorpay_payment_id": razorpay_payment_id,
        "gross_paise": int(gross_paise),
        "merchant_paise": int(merchant_paise),
        "deduction_paise": deduction,
        "fee_basis": "full_consideration",
    }
    for txn_type, paise, key in (
        ("fee_deduction", fee, f"fee_deduction:{payment_id}"),
        ("gst_deduction", gst, f"gst_deduction:{payment_id}"),
    ):
        if paise <= 0:
            continue
        await _post(
            conn,
            merchant_id=merchant_id,
            branch_id=branch_id,
            transaction_type=txn_type,
            debit_amount=_rupees(paise),
            currency="INR",
            source_type="payment",
            payment_id=payment_id,
            order_id=order_id,
            idempotency_key=key,
            metadata={**meta, "leg": txn_type},
            created_by=created_by,
        )
    return {"fee_paise": fee, "gst_paise": gst, "deduction_paise": deduction}


async def apply_payment_capture(
    *,
    conn=None,
    merchant_id,
    payment_id,
    amount,
    method=None,
    order_id=None,
    branch_id=None,
    bank_reference=None,
    razorpay_payment_id=None,
    merchant_paise: Optional[int] = None,
    payment_kind: Optional[str] = None,
    created_by=None,
) -> dict:
    """Credit the gross receipt and, for gateway captures with a frozen
    allocation, debit the fee + GST legs in the same transaction.

    All writes participate in the caller's transaction (or one opened here)
    and fail loudly so the webhook can retry.
    """
    gross_paise = rupees_to_paise(amount)
    if not merchant_id or not payment_id or gross_paise <= 0:
        raise ValueError("Capture bridge requires a merchant, canonical payment and positive amount")
    kind = payment_kind or classify_method(method)
    metadata: dict[str, Any] = {
        "razorpay_payment_id": razorpay_payment_id,
        "source": "razorpay_capture" if kind == "razorpay" else "pos_capture",
        "payment_kind": kind,
    }

    async def _run(cx) -> dict:
        await merchant_ledger_integration.post_payment_received(
            merchant_id=merchant_id,
            payment_id=payment_id,
            amount=amount,
            method=method,
            order_id=order_id,
            branch_id=branch_id,
            bank_reference=bank_reference,
            extra_metadata=metadata,
            actor_id=created_by,
            conn=cx,
            strict=True,
        )
        if kind != "razorpay":
            return {"payment_kind": kind, "fee_paise": 0, "gst_paise": 0}
        if merchant_paise is None:
            # Allocation not yet frozen (e.g. transfer recovery pending).
            # The backfill script posts the legs once it exists.
            logger.warning(
                "merchant_ledger.fee_legs_deferred",
                payment_id=str(payment_id),
                razorpay_payment_id=razorpay_payment_id,
            )
            return {"payment_kind": kind, "fee_paise": None, "gst_paise": None}
        legs = await post_fee_legs(
            conn=cx,
            merchant_id=merchant_id,
            payment_id=payment_id,
            gross_paise=gross_paise,
            merchant_paise=int(merchant_paise),
            branch_id=branch_id,
            order_id=order_id,
            razorpay_payment_id=razorpay_payment_id,
            created_by=created_by,
        )
        return {"payment_kind": kind, **legs}

    if conn is not None:
        return await _run(conn)
    async with get_transaction() as cx:
        return await _run(cx)


async def apply_settlement_processed(*, settlement_id: str, merchant_id: Optional[str] = None) -> dict:
    """Post settlement_completed (or settlement_reversed for negative lines) for
    every canonical payment Razorpay settled to this merchant's bank.

    Sources, in priority order: reconciliation lines (credit − debit per
    payment) and Route transfers back-linked to the settlement. Payments
    without a canonical local row are skipped and counted.
    """
    posted = skipped = 0
    async with get_service_connection() as conn:
        rows = await conn.fetch(
            """
            WITH lines AS (
                SELECT sp.merchant_id, sp.razorpay_payment_id,
                       SUM(COALESCE(sp.credit_paise,0) - COALESCE(sp.debit_paise,0))::bigint AS net_paise
                FROM rzp_settlement_payments sp
                WHERE sp.settlement_id = $1 AND sp.razorpay_payment_id IS NOT NULL
                  AND ($2::uuid IS NULL OR sp.merchant_id = $2::uuid)
                GROUP BY sp.merchant_id, sp.razorpay_payment_id
            ), transfers AS (
                SELECT t.merchant_id, t.razorpay_payment_id, SUM(t.amount_paise)::bigint AS net_paise
                FROM rzp_route_transfers t
                WHERE t.recipient_settlement_id = $1 AND t.status::text = 'processed'
                  AND ($2::uuid IS NULL OR t.merchant_id = $2::uuid)
                  AND NOT EXISTS (SELECT 1 FROM lines l WHERE l.razorpay_payment_id = t.razorpay_payment_id)
                GROUP BY t.merchant_id, t.razorpay_payment_id
            ), all_rows AS (
                SELECT * FROM lines UNION ALL SELECT * FROM transfers
            )
            SELECT a.merchant_id::text AS merchant_id, a.razorpay_payment_id, a.net_paise,
                   p.id::text AS payment_id, p.order_id::text AS order_id, p.branch_id::text AS branch_id,
                   s.utr
            FROM all_rows a
            LEFT JOIN payments p ON p.razorpay_payment_id = a.razorpay_payment_id
                                AND p.restaurant_id = a.merchant_id
            LEFT JOIN rzp_settlements s ON s.settlement_id = $1
            """,
            settlement_id,
            merchant_id,
        )
    for row in rows:
        if not row["payment_id"] or not row["net_paise"]:
            skipped += 1
            continue
        net = int(row["net_paise"])
        txn_type = "settlement_completed" if net > 0 else "settlement_reversed"
        amount = _rupees(abs(net))
        kwargs = dict(
            merchant_id=row["merchant_id"],
            branch_id=row["branch_id"],
            transaction_type=txn_type,
            currency="INR",
            source_type="settlement",
            payment_id=row["payment_id"],
            order_id=row["order_id"],
            utr_number=row["utr"],
            idempotency_key=f"{txn_type}:{settlement_id}:{row['payment_id']}",
            metadata={
                "settlement_id": settlement_id,
                "razorpay_payment_id": row["razorpay_payment_id"],
                "net_paise": net,
            },
        )
        if net > 0:
            kwargs["debit_amount"] = amount
        else:
            kwargs["credit_amount"] = amount
        await merchant_ledger_service.post_entry(**kwargs)
        posted += 1
    logger.info("merchant_ledger.settlement_posted", settlement_id=settlement_id, posted=posted, skipped=skipped)
    return {"settlement_id": settlement_id, "posted": posted, "skipped_without_canonical_payment": skipped}


async def post_recovery_fee_reversal(
    *,
    conn,
    merchant_id,
    payment_id,
    recovered_paise: int,
    cumulative_recovered_paise: int,
    gross_paise: int,
    merchant_paise: int,
    key_prefix: str,
    source_type: str,
    source_id=None,
    order_id=None,
    created_by=None,
) -> dict:
    """Credit back the fee + GST that Bittu no longer earns on a refunded or
    charged-back portion. Returns the merchant share of the recovery so the
    caller can report the net entitlement movement (gross − fee − gst).

    Cumulative FLOOR arithmetic mirrors Route reversals: the merchant share of
    cumulative refunds is floored, so the last refund recovers rounding.
    """
    recovered_paise = int(recovered_paise)
    cumulative = int(cumulative_recovered_paise)
    previous = cumulative - recovered_paise
    merchant_share = proportional_share_paise(
        cumulative_paise=cumulative,
        previous_cumulative_paise=previous,
        gross_paise=int(gross_paise),
        share_paise=int(merchant_paise),
    )
    deduction_share = max(recovered_paise - merchant_share, 0)
    fee_total, _ = gst_split_paise(int(gross_paise) - int(merchant_paise))
    # Floor the cumulative fee reversal the same way so fee + gst == deduction share.
    fee_now = proportional_share_paise(
        cumulative_paise=cumulative, previous_cumulative_paise=previous,
        gross_paise=int(gross_paise), share_paise=fee_total,
    )
    gst_now = deduction_share - fee_now
    if gst_now < 0:
        gst_now, fee_now = 0, deduction_share
    meta = {
        "recovered_paise": recovered_paise,
        "cumulative_recovered_paise": cumulative,
        "merchant_share_paise": merchant_share,
        "deduction_share_paise": deduction_share,
        "fee_basis": "full_consideration",
    }
    for txn_type, paise, key in (
        ("fee_reversal", fee_now, f"{key_prefix}_fee_reversal"),
        ("gst_reversal", gst_now, f"{key_prefix}_gst_reversal"),
    ):
        if paise <= 0:
            continue
        await _post(
            conn,
            merchant_id=merchant_id,
            transaction_type=txn_type,
            credit_amount=_rupees(paise),
            currency="INR",
            source_type=source_type,
            source_id=source_id,
            payment_id=payment_id,
            order_id=order_id,
            idempotency_key=key,
            metadata={**meta, "leg": txn_type},
            created_by=created_by,
        )
    return {"merchant_share_paise": merchant_share, "fee_paise": fee_now, "gst_paise": gst_now}


async def frozen_allocation_for_payment(conn, payment_id) -> Optional[dict]:
    """Return {gross_paise, merchant_paise, razorpay_payment_id} for a canonical
    payment with a frozen Route allocation, else None (cash/direct/unresolved)."""
    row = await conn.fetchrow(
        """
        SELECT p.razorpay_payment_id, a.gross_paise, a.merchant_paise
        FROM payments p
        JOIN rzp_route_allocations a ON a.razorpay_payment_id = p.razorpay_payment_id
        WHERE p.id = $1::uuid
        """,
        str(payment_id),
    )
    if not row or row["merchant_paise"] is None:
        return None
    return {
        "razorpay_payment_id": row["razorpay_payment_id"],
        "gross_paise": int(row["gross_paise"]),
        "merchant_paise": int(row["merchant_paise"]),
    }