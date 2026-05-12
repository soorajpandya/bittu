"""
Escrow Ledger Integration helpers — best-effort wrappers that wire the
existing payment / settlement code paths to the escrow_ledger.

DESIGN CONTRACT (identical to merchant_ledger_integration)
─────────────────────────────────────────────────────────
1. Best-effort. A ledger failure is LOGGED but NEVER re-raised — the
   upstream payment/settlement transaction is the source of truth.
2. Deterministic idempotency keys — replays are no-ops at the DB level.
3. Pass `conn=` to participate in an outer transaction; otherwise opens
   its own.
4. No-ops when merchant_id is missing or amount <= 0.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from app.core.logging import get_logger
from app.services.escrow_service import escrow_service

logger = get_logger(__name__)


def _to_decimal(v) -> Decimal:
    if v is None:
        return Decimal("0")
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal("0")


# ════════════════════════════════════════════════════════════════════════
# HOLD on payment captured
# ════════════════════════════════════════════════════════════════════════
async def hold_payment_in_escrow(
    *,
    merchant_id: Optional[str],
    payment_id: str,
    amount,
    method: Optional[str] = None,
    order_id: Optional[str] = None,
    branch_id: Optional[str] = None,
    actor_id: Optional[str] = None,
    extra_metadata: Optional[dict[str, Any]] = None,
    conn=None,
) -> None:
    """CREDIT escrow_hold for a captured payment. Idempotent on payment_id."""
    if not merchant_id or not payment_id:
        return
    amt = _to_decimal(amount)
    if amt <= 0:
        return
    metadata = {"method": method, **(extra_metadata or {})}
    try:
        await escrow_service.hold_for_payment(
            merchant_id=merchant_id,
            branch_id=branch_id,
            payment_id=payment_id,
            order_id=order_id,
            amount=amt,
            metadata=metadata,
            created_by=actor_id,
            conn=conn,
        )
    except Exception as exc:
        logger.error(
            "escrow_ledger.hold.failed",
            extra={
                "payment_id":  str(payment_id),
                "merchant_id": str(merchant_id),
                "amount":      float(amt),
                "error":       str(exc),
            },
        )


# ════════════════════════════════════════════════════════════════════════
# RELEASE on settlement settled
# ════════════════════════════════════════════════════════════════════════
async def release_holds_for_settlement(
    *,
    settlement_row: dict,
    actor_id: Optional[str] = None,
    conn=None,
) -> None:
    """
    On a settlement transitioning to `settled`, release escrow holds for
    every payment that was rolled into that batch.

    Best-effort; per-payment failures are logged but do not stop the rest.
    """
    sid = settlement_row.get("id")
    merchant_id = settlement_row.get("restaurant_id")
    if not sid or not merchant_id:
        return

    sid_str = str(sid)
    bank_ref = settlement_row.get("bank_reference_number")

    # Pull payment_ids that participated in this settlement batch.
    try:
        from app.core.database import get_connection
        async with get_connection() as cx:
            rows = await cx.fetch(
                "SELECT payment_id, gross_amount FROM bittu_settlement_transactions "
                "WHERE settlement_id = $1::uuid AND payment_id IS NOT NULL",
                sid_str,
            )
    except Exception as exc:
        logger.error(
            "escrow_ledger.release.lookup_failed",
            extra={"settlement_id": sid_str, "error": str(exc)},
        )
        return

    for r in rows:
        payment_id = r["payment_id"]
        gross = _to_decimal(r["gross_amount"])
        if not payment_id or gross <= 0:
            continue
        try:
            # Find the originating escrow_hold for this payment.
            from app.core.database import get_connection
            async with get_connection() as cx:
                hold = await cx.fetchrow(
                    """
                    SELECT id, credit_amount FROM escrow_ledger
                     WHERE merchant_id = $1::uuid
                       AND payment_id  = $2::uuid
                       AND transaction_type = 'escrow_hold'
                     ORDER BY created_at DESC LIMIT 1
                    """,
                    str(merchant_id), str(payment_id),
                )
            if hold is None:
                # No hold was posted (e.g. payment predates Phase 2) — skip.
                continue
            await escrow_service.release_hold(
                merchant_id=merchant_id,
                hold_entry_id=hold["id"],
                amount=hold["credit_amount"],
                settlement_id=sid_str,
                bank_reference=bank_ref,
                reason="settlement_settled",
                metadata={
                    "settlement_reference": settlement_row.get("settlement_reference"),
                    "payment_id": str(payment_id),
                },
                created_by=actor_id,
                conn=conn,
            )
        except Exception as exc:
            # Most common: unique-violation on escrow_release_links because
            # the hold was already released (idempotent re-run). That's fine.
            msg = str(exc)
            if "escrow_release_links" in msg or "duplicate key" in msg.lower():
                logger.info(
                    "escrow_ledger.release.already_done",
                    extra={"settlement_id": sid_str, "payment_id": str(payment_id)},
                )
            else:
                logger.error(
                    "escrow_ledger.release.failed",
                    extra={
                        "settlement_id": sid_str,
                        "payment_id":    str(payment_id),
                        "error":         msg,
                    },
                )
