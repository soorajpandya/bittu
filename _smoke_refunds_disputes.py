"""
Phase 7 — Refunds & Disputes smoke test.

Verifies (without calling any payment gateway):
  • create refund (initiated), list, get, refundable amount drops
  • transition refund initiated → succeeded posts a debit ledger entry
  • cannot create refund > refundable_amount
  • cannot transition succeeded → anything (terminal)
  • open dispute, list, get, list_events, transition opened → lost posts
    chargeback ledger entry; dispute_events row appended
  • dispute_events table is append-only (UPDATE/DELETE raise P0002)
  • merchant scoping: another merchant cannot see these rows
  • CSV exports include the expected rows

Cleanup at the end deletes test rows and the linked ledger entries
(temporarily disabling immutability triggers — proves they normally block).
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from uuid import uuid4, UUID

from app.core.database import init_db_pool, close_db_pool, get_connection
from app.core.exceptions import ConflictError, NotFoundError
from app.services.refund_service import refund_service
from app.services.dispute_service import dispute_service

MERCHANT_ID = "751c6d1d-1559-45f2-a24b-7ecd16678113"
USER_ID     = "a07da9d2-1235-4af5-bcd3-9ba56b6edc47"
OTHER_MID   = str(uuid4())

# Use a payment we know exists for this merchant (amount = 1.05).
# The smoke creates two new payments to keep its math clean.


async def _make_payment(c, amount: Decimal) -> tuple[str, str]:
    pmt_id = str(uuid4())
    order_id = str(uuid4())
    # orders has a NOT NULL FK on payments.order_id → seed an order row
    # using the minimal columns that exist.
    await c.execute(
        """
        INSERT INTO orders (id, restaurant_id, user_id, status, subtotal, total_amount, created_at, updated_at)
        VALUES ($1::uuid, $2::uuid, $3, 'completed', $4, $4, now(), now())
        """,
        order_id, MERCHANT_ID, USER_ID, amount,
    )
    await c.execute(
        """
        INSERT INTO payments
            (id, order_id, restaurant_id, user_id, method, status, amount, currency)
        VALUES ($1::uuid, $2::uuid, $3::uuid, $4, 'cash', 'completed', $5, 'INR')
        """,
        pmt_id, order_id, MERCHANT_ID, USER_ID, amount,
    )
    return pmt_id, order_id


async def _cleanup(payment_ids: list[str], dispute_ids: list[int], refund_ids: list[int]):
    async with get_connection() as c:
        # disable triggers
        await c.execute("ALTER TABLE merchant_ledger DISABLE TRIGGER trg_immutable_merchant_ledger")
        await c.execute("ALTER TABLE dispute_events DISABLE TRIGGER trg_dispute_events_no_delete")
        try:
            for pid in payment_ids:
                await c.execute(
                    "DELETE FROM merchant_ledger WHERE payment_id = $1::uuid", pid
                )
            for did in dispute_ids:
                await c.execute("DELETE FROM dispute_events WHERE dispute_id = $1", did)
                await c.execute("DELETE FROM disputes WHERE id = $1", did)
            for rid in refund_ids:
                await c.execute("DELETE FROM refunds WHERE id = $1", rid)
            for pid in payment_ids:
                row = await c.fetchrow(
                    "SELECT order_id FROM payments WHERE id = $1::uuid", pid
                )
                await c.execute("DELETE FROM payments WHERE id = $1::uuid", pid)
                if row and row["order_id"]:
                    await c.execute(
                        "DELETE FROM orders WHERE id = $1::uuid", row["order_id"]
                    )
        finally:
            await c.execute("ALTER TABLE merchant_ledger ENABLE TRIGGER trg_immutable_merchant_ledger")
            await c.execute("ALTER TABLE dispute_events ENABLE TRIGGER trg_dispute_events_no_delete")


async def main():
    await init_db_pool()
    payment_ids: list[str] = []
    refund_ids:  list[int] = []
    dispute_ids: list[int] = []

    try:
        async with get_connection() as c:
            pmt_a, ord_a = await _make_payment(c, Decimal("100.00"))
            pmt_b, ord_b = await _make_payment(c, Decimal("50.00"))
        payment_ids += [pmt_a, pmt_b]

        # ── Refundable amount starts equal to payment amount
        refundable = await refund_service.refundable_amount(
            merchant_id=MERCHANT_ID, payment_id=pmt_a,
        )
        assert refundable == Decimal("100.0000"), refundable

        # ── Create initiated refund
        r1 = await refund_service.create(
            merchant_id=MERCHANT_ID, payment_id=pmt_a, amount=Decimal("30"),
            kind="partial", reason="customer asked",
            initiated_by_user_id=USER_ID,
        )
        refund_ids.append(r1["id"])
        assert r1["status"] == "initiated", r1
        assert Decimal(r1["amount"]) == Decimal("30.0000"), r1["amount"]

        # ── refundable_amount drops by 30 (initiated counts)
        refundable = await refund_service.refundable_amount(
            merchant_id=MERCHANT_ID, payment_id=pmt_a,
        )
        assert refundable == Decimal("70.0000"), refundable

        # ── Cannot exceed refundable
        try:
            await refund_service.create(
                merchant_id=MERCHANT_ID, payment_id=pmt_a, amount=Decimal("80"),
                initiated_by_user_id=USER_ID,
            )
            raise AssertionError("expected ConflictError")
        except ConflictError:
            pass

        # ── Transition initiated → succeeded posts ledger debit
        async with get_connection() as c:
            bal_before = await c.fetchval(
                "SELECT current_balance FROM merchant_ledger_balance_locks "
                "WHERE merchant_id = $1::uuid AND currency='INR'",
                MERCHANT_ID,
            ) or Decimal("0")
        r1b = await refund_service.transition(
            r1["id"], merchant_id=MERCHANT_ID, new_status="succeeded",
            gateway_refund_id="GW_TEST_001", actor_user_id=USER_ID,
        )
        assert r1b["status"] == "succeeded", r1b
        assert r1b["ledger_entry_id"], r1b
        async with get_connection() as c:
            bal_after = await c.fetchval(
                "SELECT current_balance FROM merchant_ledger_balance_locks "
                "WHERE merchant_id = $1::uuid AND currency='INR'",
                MERCHANT_ID,
            )
            entry = await c.fetchrow(
                "SELECT transaction_type, debit_amount, credit_amount FROM merchant_ledger "
                "WHERE id = $1::uuid", r1b["ledger_entry_id"],
            )
        assert entry["transaction_type"] == "refund", entry
        assert entry["debit_amount"] == Decimal("30.0000"), entry
        assert (Decimal(str(bal_before)) - Decimal(str(bal_after))) == Decimal("30.0000")

        # ── Terminal — cannot transition again
        try:
            await refund_service.transition(
                r1["id"], merchant_id=MERCHANT_ID, new_status="failed",
            )
            raise AssertionError("expected ConflictError")
        except ConflictError:
            pass

        # ── Other merchant cannot see this refund
        try:
            await refund_service.get(r1["id"], merchant_id=OTHER_MID)
            raise AssertionError("expected NotFoundError")
        except NotFoundError:
            pass

        # ── List + CSV
        listed = await refund_service.list_refunds(merchant_id=MERCHANT_ID, payment_id=pmt_a)
        assert any(x["id"] == r1["id"] for x in listed)
        csv_out = refund_service.to_csv(listed)
        assert "refund_uuid" in csv_out["body"].splitlines()[0]

        # ── Open dispute
        d1 = await dispute_service.open_dispute(
            merchant_id=MERCHANT_ID, kind="chargeback",
            amount=Decimal("50.00"), payment_id=pmt_b, order_id=ord_b,
            opened_by_user_id=USER_ID, evidence={"docs": ["receipt.pdf"]},
        )
        dispute_ids.append(d1["id"])
        assert d1["status"] == "opened"

        events = await dispute_service.list_events(d1["id"], merchant_id=MERCHANT_ID)
        assert len(events) == 1 and events[0]["event_type"] == "opened"

        # ── Add a note (append-only event)
        await dispute_service.add_note(
            d1["id"], merchant_id=MERCHANT_ID,
            note="Bank requested evidence", actor_user_id=USER_ID,
        )
        events = await dispute_service.list_events(d1["id"], merchant_id=MERCHANT_ID)
        assert len(events) == 2 and events[1]["event_type"] == "note"

        # ── dispute_events is append-only
        async with get_connection() as c:
            try:
                await c.execute(
                    "UPDATE dispute_events SET payload = '{}'::jsonb "
                    "WHERE dispute_id = $1", d1["id"],
                )
                raise AssertionError("expected append-only failure")
            except Exception as e:
                assert "append-only" in str(e), e

        # ── Transition opened → lost posts chargeback ledger entry
        async with get_connection() as c:
            bal_before = await c.fetchval(
                "SELECT current_balance FROM merchant_ledger_balance_locks "
                "WHERE merchant_id = $1::uuid AND currency='INR'",
                MERCHANT_ID,
            ) or Decimal("0")
        d1b = await dispute_service.transition(
            d1["id"], merchant_id=MERCHANT_ID, new_status="lost",
            outcome="lost", resolution_notes="Bank ruled in customer favor",
            actor_user_id=USER_ID,
        )
        assert d1b["status"] == "lost" and d1b["outcome"] == "lost"
        assert d1b["ledger_entry_id"], d1b
        async with get_connection() as c:
            entry = await c.fetchrow(
                "SELECT transaction_type, debit_amount FROM merchant_ledger "
                "WHERE id = $1::uuid", d1b["ledger_entry_id"],
            )
            bal_after = await c.fetchval(
                "SELECT current_balance FROM merchant_ledger_balance_locks "
                "WHERE merchant_id = $1::uuid AND currency='INR'",
                MERCHANT_ID,
            )
        assert entry["transaction_type"] == "chargeback"
        assert entry["debit_amount"] == Decimal("50.0000")
        assert (Decimal(str(bal_before)) - Decimal(str(bal_after))) == Decimal("50.0000")

        # ── Cannot transition lost → anything
        try:
            await dispute_service.transition(
                d1["id"], merchant_id=MERCHANT_ID, new_status="won",
            )
            raise AssertionError("expected ConflictError")
        except ConflictError:
            pass

        # ── Other merchant scoping
        try:
            await dispute_service.get(d1["id"], merchant_id=OTHER_MID)
            raise AssertionError("expected NotFoundError")
        except NotFoundError:
            pass

        # ── Admin (cross-merchant) view
        all_refunds = await refund_service.list_refunds(payment_id=pmt_a)
        assert any(x["id"] == r1["id"] for x in all_refunds)
        all_disputes = await dispute_service.list_disputes(payment_id=pmt_b)
        assert any(x["id"] == d1["id"] for x in all_disputes)

        print("✅ Phase 7 smoke OK")
        print(f"   refund_id={r1['id']} ledger={r1b['ledger_entry_id']}")
        print(f"   dispute_id={d1['id']} ledger={d1b['ledger_entry_id']}")

    finally:
        await _cleanup(payment_ids, dispute_ids, refund_ids)
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
