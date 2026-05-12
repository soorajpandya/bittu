"""
Phase 8 — Reporting & Analytics smoke test.

Seeds a controlled set of orders / payments / refunds / disputes for today,
then verifies:
  • on-the-fly summaries (pnl, settlement_summary, refund_summary, dispute_summary)
  • compute_daily_rollup → merchant_daily_rollups row matches
  • daily_rollups + monthly_rollups read back correctly
  • merchant scoping (other merchant cannot see these rows)
  • CSV exports include the headers
  • Idempotent recompute bumps source_version, doesn't dup rows

Cleanup: deletes seeded rows + the rollup row.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from app.core.database import init_db_pool, close_db_pool, get_connection
from app.services.refund_service import refund_service
from app.services.dispute_service import dispute_service
from app.services.reporting_service import reporting_service

MERCHANT_ID = "751c6d1d-1559-45f2-a24b-7ecd16678113"
USER_ID     = "a07da9d2-1235-4af5-bcd3-9ba56b6edc47"
OTHER_MID   = str(uuid4())
TODAY       = date.today()


async def _seed_payment(c, amount: Decimal) -> tuple[str, str]:
    pmt_id = str(uuid4())
    order_id = str(uuid4())
    await c.execute(
        """
        INSERT INTO orders (id, restaurant_id, user_id, status,
                            subtotal, total_amount, discount_amount, tax_amount,
                            cost_of_goods_sold, created_at, updated_at)
        VALUES ($1::uuid, $2::uuid, $3, 'completed',
                $4, $4, 0, 0, 0, now(), now())
        """,
        order_id, MERCHANT_ID, USER_ID, amount,
    )
    await c.execute(
        """
        INSERT INTO payments
            (id, order_id, restaurant_id, user_id, method, status, amount, currency, created_at)
        VALUES ($1::uuid, $2::uuid, $3::uuid, $4, 'cash', 'completed', $5, 'INR', now())
        """,
        pmt_id, order_id, MERCHANT_ID, USER_ID, amount,
    )
    return pmt_id, order_id


async def _cleanup(payment_ids, refund_ids, dispute_ids):
    async with get_connection() as c:
        await c.execute("ALTER TABLE merchant_ledger DISABLE TRIGGER trg_immutable_merchant_ledger")
        await c.execute("ALTER TABLE dispute_events DISABLE TRIGGER trg_dispute_events_no_delete")
        try:
            for pid in payment_ids:
                await c.execute("DELETE FROM merchant_ledger WHERE payment_id = $1::uuid", pid)
            for did in dispute_ids:
                await c.execute("DELETE FROM dispute_events WHERE dispute_id = $1", did)
                await c.execute("DELETE FROM disputes WHERE id = $1", did)
            for rid in refund_ids:
                await c.execute("DELETE FROM refunds WHERE id = $1", rid)
            for pid in payment_ids:
                row = await c.fetchrow("SELECT order_id FROM payments WHERE id = $1::uuid", pid)
                await c.execute("DELETE FROM payments WHERE id = $1::uuid", pid)
                if row and row["order_id"]:
                    await c.execute("DELETE FROM orders WHERE id = $1::uuid", row["order_id"])
            await c.execute(
                "DELETE FROM merchant_daily_rollups WHERE merchant_id = $1::uuid AND rollup_date = $2",
                MERCHANT_ID, TODAY,
            )
        finally:
            await c.execute("ALTER TABLE merchant_ledger ENABLE TRIGGER trg_immutable_merchant_ledger")
            await c.execute("ALTER TABLE dispute_events ENABLE TRIGGER trg_dispute_events_no_delete")


async def main():
    await init_db_pool()
    payment_ids: list[str] = []
    refund_ids:  list[int] = []
    dispute_ids: list[int] = []

    # snapshot existing values for today so we can compare deltas, not absolutes
    async with get_connection() as c:
        snap_pnl = await reporting_service.pnl(
            merchant_id=MERCHANT_ID, from_date=TODAY, to_date=TODAY,
        )
        snap_ref = await reporting_service.refund_summary(
            merchant_id=MERCHANT_ID, from_date=TODAY, to_date=TODAY,
        )
        snap_disp = await reporting_service.dispute_summary(
            merchant_id=MERCHANT_ID, from_date=TODAY, to_date=TODAY,
        )

    try:
        async with get_connection() as c:
            # 3 orders × 100 = 300 gross
            for _ in range(3):
                pid, oid = await _seed_payment(c, Decimal("100.00"))
                payment_ids.append(pid)

        # ── refund 40 on payment[0] → succeeded → ledger debit 40
        r1 = await refund_service.create(
            merchant_id=MERCHANT_ID, payment_id=payment_ids[0], amount=Decimal("40"),
            kind="partial", reason="smoke", initiated_by_user_id=USER_ID,
        )
        refund_ids.append(r1["id"])
        await refund_service.transition(
            r1["id"], merchant_id=MERCHANT_ID, new_status="succeeded",
            actor_user_id=USER_ID,
        )

        # ── dispute 25 on payment[1] → lost → ledger chargeback 25
        d1 = await dispute_service.open_dispute(
            merchant_id=MERCHANT_ID, kind="chargeback", amount=Decimal("25"),
            payment_id=payment_ids[1], opened_by_user_id=USER_ID,
        )
        dispute_ids.append(d1["id"])
        await dispute_service.transition(
            d1["id"], merchant_id=MERCHANT_ID, new_status="lost",
            outcome="lost", actor_user_id=USER_ID,
        )

        # ── on-the-fly P&L deltas
        pnl = await reporting_service.pnl(
            merchant_id=MERCHANT_ID, from_date=TODAY, to_date=TODAY,
        )
        d_orders   = pnl["orders_count"] - snap_pnl["orders_count"]
        d_gross    = pnl["gross_sales"] - snap_pnl["gross_sales"]
        d_refunds  = pnl["refunds_amount"] - snap_pnl["refunds_amount"]
        d_chargeb  = pnl["chargebacks_amount"] - snap_pnl["chargebacks_amount"]
        assert d_orders  == 3,                     d_orders
        assert d_gross   == Decimal("300.0000"),   d_gross
        assert d_refunds == Decimal("40.0000"),    d_refunds
        assert d_chargeb == Decimal("25.0000"),    d_chargeb

        # ── refund summary deltas
        ref = await reporting_service.refund_summary(
            merchant_id=MERCHANT_ID, from_date=TODAY, to_date=TODAY,
        )
        assert (ref["total_count"]      - snap_ref["total_count"])      == 1
        assert (ref["succeeded_count"]  - snap_ref["succeeded_count"])  == 1
        assert (ref["succeeded_amount"] - snap_ref["succeeded_amount"]) == Decimal("40.0000")

        # ── dispute summary deltas
        disp = await reporting_service.dispute_summary(
            merchant_id=MERCHANT_ID, from_date=TODAY, to_date=TODAY,
        )
        assert (disp["total_count"] - snap_disp["total_count"]) == 1
        assert (disp["lost_count"]  - snap_disp["lost_count"])  == 1
        assert (disp["lost_amount"] - snap_disp["lost_amount"]) == Decimal("25.0000")

        # ── compute daily rollup
        roll = await reporting_service.compute_daily_rollup(
            merchant_id=MERCHANT_ID, rollup_date=TODAY, currency="INR",
        )
        assert roll["orders_count"]             >= 3
        assert Decimal(str(roll["gross_sales"])) >= Decimal("300")
        assert Decimal(str(roll["refunds_succeeded_amount"])) >= Decimal("40")
        assert Decimal(str(roll["disputes_lost_amount"]))     >= Decimal("25")
        assert Decimal(str(roll["chargebacks_total"]))        >= Decimal("25")
        v1 = roll["source_version"]

        # idempotent recompute bumps version
        roll2 = await reporting_service.compute_daily_rollup(
            merchant_id=MERCHANT_ID, rollup_date=TODAY, currency="INR",
        )
        assert roll2["source_version"] == v1 + 1

        # exactly one row in table for (merchant, today, INR)
        async with get_connection() as c:
            n = await c.fetchval(
                "SELECT COUNT(*) FROM merchant_daily_rollups "
                "WHERE merchant_id = $1::uuid AND rollup_date = $2 AND currency = 'INR'",
                MERCHANT_ID, TODAY,
            )
        assert n == 1, n

        # ── daily_rollups read-back
        daily = await reporting_service.daily_rollups(
            merchant_id=MERCHANT_ID, from_date=TODAY, to_date=TODAY,
        )
        assert len(daily) == 1
        assert daily[0]["rollup_date"] == TODAY.isoformat()

        # ── monthly_rollups
        monthly = await reporting_service.monthly_rollups(
            merchant_id=MERCHANT_ID,
            from_date=TODAY.replace(day=1),
            to_date=TODAY,
        )
        assert any(m["month_start"] == TODAY.replace(day=1).isoformat() for m in monthly)

        # ── merchant scoping: another merchant sees zero from our seeds
        other = await reporting_service.pnl(
            merchant_id=OTHER_MID, from_date=TODAY, to_date=TODAY,
        )
        assert other["orders_count"] == 0
        assert other["gross_sales"]  == Decimal("0")

        other_daily = await reporting_service.daily_rollups(
            merchant_id=OTHER_MID, from_date=TODAY, to_date=TODAY,
        )
        assert other_daily == []

        # ── admin (no merchant_id) sees totals ≥ our deltas
        admin_pnl = await reporting_service.pnl(
            merchant_id=None, from_date=TODAY, to_date=TODAY,
        )
        assert admin_pnl["orders_count"]   >= pnl["orders_count"]
        assert admin_pnl["refunds_amount"] >= pnl["refunds_amount"]

        # ── CSV exports
        csv_pnl = reporting_service.dict_to_csv(pnl, filename="pnl.csv")
        assert "gross_sales" in csv_pnl["body"].splitlines()[0]
        csv_daily = reporting_service.to_csv(daily, filename="daily.csv")
        assert "rollup_date" in csv_daily["body"].splitlines()[0]

        print("✅ Phase 8 smoke OK")
        print(f"   pnl_delta gross=300 refunds=40 chargebacks=25")
        print(f"   rollup id={roll2['id']} source_version={roll2['source_version']}")

    finally:
        await _cleanup(payment_ids, refund_ids, dispute_ids)
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
