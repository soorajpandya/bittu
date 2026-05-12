"""
Smoke test for merchant_ledger integration helpers.

Verifies:
  1. post_payment_received credits the ledger (idempotent)
  2. post_settlement_settled posts 3 debit legs that sum to gross
  3. post_settlement_reversed posts compensating credits
  4. Re-running each helper with same key is a no-op (idempotency)
  5. Final balance after settled+reversed equals balance after just receipt
"""
import asyncio
import os
import uuid
from decimal import Decimal

from app.core.database import init_db_pool, close_db_pool, get_connection
from app.services.merchant_ledger_integration import (
    post_payment_received,
    post_settlement_settled,
    post_settlement_reversed,
)
from app.services.merchant_ledger_service import merchant_ledger_service

MERCHANT_ID = "751c6d1d-1559-45f2-a24b-7ecd16678113"
BRANCH_ID   = "82ac3013-14cb-4e1a-acd8-3d5f921dafb8"
ACTOR_ID    = "a07da9d2-1235-4af5-bcd3-9ba56b6edc47"


async def _balance() -> Decimal:
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT current_balance FROM merchant_ledger_balance_locks "
            "WHERE merchant_id = $1 AND currency = 'INR'",
            uuid.UUID(MERCHANT_ID),
        )
    return Decimal(str(row["current_balance"])) if row else Decimal("0")


async def main():
    await init_db_pool()
    try:
        b0 = await _balance()
        print(f"[start] balance = {b0}")

        # 1. Cash receipt
        pid = str(uuid.uuid4())
        oid = str(uuid.uuid4())
        await post_payment_received(
            merchant_id=MERCHANT_ID,
            payment_id=pid,
            amount=Decimal("250.00"),
            method="cash",
            order_id=oid,
            branch_id=BRANCH_ID,
            actor_id=ACTOR_ID,
        )
        b1 = await _balance()
        assert b1 - b0 == Decimal("250.0000"), f"expected +250, got {b1 - b0}"
        print(f"[pass] payment_received credited 250 (balance={b1})")

        # 2. Idempotency replay
        await post_payment_received(
            merchant_id=MERCHANT_ID,
            payment_id=pid,
            amount=Decimal("250.00"),
            method="cash",
            order_id=oid,
            branch_id=BRANCH_ID,
            actor_id=ACTOR_ID,
        )
        b2 = await _balance()
        assert b2 == b1, f"replay changed balance: {b1} -> {b2}"
        print(f"[pass] payment_received is idempotent")

        # 3. Settlement settled (synthetic settlement_id)
        sid = str(uuid.uuid4())
        synthetic_row = {
            "id": sid,
            "restaurant_id": MERCHANT_ID,
            "branch_id": BRANCH_ID,
            "settlement_reference": f"SMOKE-{sid[:8]}",
            "gross_amount": Decimal("1000.00"),
            "bittu_fee_amount": Decimal("2.5420"),
            "gst_amount": Decimal("0.4576"),
            "net_settlement_amount": Decimal("997.0004"),
            "bank_reference_number": "TEST-UTR-001",
        }
        await post_settlement_settled(settlement_row=synthetic_row, actor_id=ACTOR_ID)
        b3 = await _balance()
        delta = b3 - b2
        # Expect debit of net + fee + gst = 997.0004 + 2.5420 + 0.4576 = 1000.0000
        assert delta == Decimal("-1000.0000"), f"expected -1000, got {delta}"
        print(f"[pass] settlement_settled debited 1000 (balance={b3})")

        # 4. Idempotency on settlement_settled
        await post_settlement_settled(settlement_row=synthetic_row, actor_id=ACTOR_ID)
        b4 = await _balance()
        assert b4 == b3, f"settlement replay changed balance: {b3} -> {b4}"
        print(f"[pass] settlement_settled is idempotent")

        # 5. Settlement reversed → restores balance
        await post_settlement_reversed(settlement_row=synthetic_row, actor_id=ACTOR_ID)
        b5 = await _balance()
        delta_rev = b5 - b4
        assert delta_rev == Decimal("1000.0000"), f"expected +1000, got {delta_rev}"
        assert b5 == b2, f"net effect not zero: started {b2}, ended {b5}"
        print(f"[pass] settlement_reversed credited 1000 (balance={b5})")

        # 6. Idempotency on reversed
        await post_settlement_reversed(settlement_row=synthetic_row, actor_id=ACTOR_ID)
        b6 = await _balance()
        assert b6 == b5
        print(f"[pass] settlement_reversed is idempotent")

        # 7. Consistency check
        async with get_connection() as conn:
            report = await conn.fetchval(
                "SELECT fn_check_merchant_ledger_consistency($1, 'INR')",
                uuid.UUID(MERCHANT_ID),
            )
        print(f"[consistency] {report}")

        print("\n[ok] all integration smoke checks passed")
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
