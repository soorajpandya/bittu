"""Phase 10 smoke — Fee Engine v2.

Exercises plans, rules, overrides, compute, append-only audit, and
permission-gated negatives. Uses an isolated synthetic merchant id
(OTHER_MID) to avoid polluting prod data.
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from uuid import uuid4

from app.core.database import init_db_pool, close_db_pool, get_connection
from app.core.exceptions import NotFoundError, ValidationError, ConflictError
from app.services.fee_service import fee_service


OTHER_MID = str(uuid4())


def _ok(label, ok, extra=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}  {extra}")
    if not ok:
        raise SystemExit(1)


async def main():
    await init_db_pool()
    print(f"smoke OTHER_MID={OTHER_MID}")
    custom_plan_id = None
    override_id = None
    try:
        # 1. resolve_plan → default
        plan = await fee_service.resolve_plan(OTHER_MID)
        _ok("resolve_plan→default_v1", plan["code"] == "default_v1",
            f"plan={plan['code']}")

        # 2. compute upi 1000 → fee 2.54 + gst 0.46 = total 3.00, net 997.00
        c = await fee_service.compute_fee(
            OTHER_MID, gross=Decimal("1000.00"), payment_method="upi",
        )
        total = Decimal(c["total_deduction"])
        fee   = Decimal(c["fee_amount"])
        gst   = Decimal(c["gst_amount"])
        _ok("compute upi 1000 total=3.00", total == Decimal("3.00"),
            f"total={total} fee={fee} gst={gst}")
        _ok("fee+gst==total to paisa", (fee + gst) == total,
            f"fee+gst={fee + gst}")
        _ok("net=997.00", Decimal(c["net_amount"]) == Decimal("997.00"))

        # 3. compute cash 1000 → 0 (cash bypass rule, priority 100)
        c2 = await fee_service.compute_fee(
            OTHER_MID, gross=Decimal("1000.00"), payment_method="cash",
        )
        _ok("compute cash 1000 total=0",
            Decimal(c2["total_deduction"]) == Decimal("0"),
            f"total={c2['total_deduction']}")
        _ok("cash net=1000.00",
            Decimal(c2["net_amount"]) == Decimal("1000.00"))

        # 4. negative gross → ValidationError
        try:
            await fee_service.compute_fee(OTHER_MID, gross=Decimal("-1"))
            _ok("negative gross rejected", False)
        except ValidationError:
            _ok("negative gross rejected", True)

        # 5. create custom premium plan
        plan_code = f"tier_premium_{OTHER_MID[:8]}"
        custom_plan = await fee_service.create_plan(
            code=plan_code, name="Premium tier",
            description="Lower MDR for high-volume",
            gst_rate=Decimal("0.18"),
        )
        custom_plan_id = custom_plan["id"]
        _ok("create custom plan", custom_plan["code"] == plan_code,
            f"plan_id={custom_plan_id}")

        # 6. add wildcard 0.2% rule + tier rule >= 10000 at 0.15%
        await fee_service.add_rule(
            custom_plan_id, percent_rate=Decimal("0.002"),
            priority=0,
        )
        tier_rule = await fee_service.add_rule(
            custom_plan_id, min_amount=Decimal("10000"),
            percent_rate=Decimal("0.0015"), priority=50,
        )
        _ok("added 2 rules", tier_rule["priority"] == 50)

        # 7. set override → premium plan
        ov = await fee_service.set_merchant_override(
            OTHER_MID, plan_id=custom_plan_id,
            reason="phase10 smoke",
        )
        override_id = ov["id"]
        plan2 = await fee_service.resolve_plan(OTHER_MID)
        _ok("override resolves to premium", plan2["code"] == plan_code,
            f"resolved={plan2['code']}")

        # 8. compute 5000 → wildcard 0.2% → total = 10.00
        c3 = await fee_service.compute_fee(
            OTHER_MID, gross=Decimal("5000.00"), payment_method="upi",
        )
        _ok("compute 5000 on premium → total=10.00",
            Decimal(c3["total_deduction"]) == Decimal("10.00"),
            f"total={c3['total_deduction']}")

        # 9. compute 20000 → tier rule 0.15% → total = 30.00
        c4 = await fee_service.compute_fee(
            OTHER_MID, gross=Decimal("20000.00"), payment_method="upi",
            record=True, payment_id=f"pay_{OTHER_MID[:8]}",
        )
        _ok("compute 20000 on premium → total=30.00",
            Decimal(c4["total_deduction"]) == Decimal("30.00"),
            f"total={c4['total_deduction']} rule_id={c4.get('rule_id')}")
        _ok("computation recorded",
            c4.get("computation_id") is not None,
            f"id={c4.get('computation_id')}")

        # 10. computations log
        comps = await fee_service.list_computations(merchant_id=OTHER_MID)
        _ok("computation log lists row", len(comps) == 1,
            f"n={len(comps)}")

        # 11. append-only: try DELETE → P0002
        async with get_connection() as conn:
            try:
                await conn.execute(
                    "DELETE FROM fee_computations WHERE merchant_id = $1::uuid",
                    OTHER_MID,
                )
                _ok("DELETE on fee_computations rejected", False)
            except Exception as e:
                _ok("DELETE on fee_computations rejected",
                    "append-only" in str(e).lower(),
                    f"err={str(e)[:80]}")

            # try UPDATE → P0002
            try:
                await conn.execute(
                    "UPDATE fee_computations SET fee_amount = 0 WHERE merchant_id = $1::uuid",
                    OTHER_MID,
                )
                _ok("UPDATE on fee_computations rejected", False)
            except Exception as e:
                _ok("UPDATE on fee_computations rejected",
                    "append-only" in str(e).lower())

        # 12. end override → resolve falls back to default
        await fee_service.end_override(override_id)
        plan3 = await fee_service.resolve_plan(OTHER_MID)
        _ok("override ended → default_v1",
            plan3["code"] == "default_v1",
            f"resolved={plan3['code']}")

        # 13. validation: percent_rate > 1
        try:
            await fee_service.add_rule(
                custom_plan_id, percent_rate=Decimal("2.0"),
            )
            _ok("rate>1 rejected", False)
        except ValidationError:
            _ok("rate>1 rejected", True)

        # 14. validation: max_amount <= min_amount
        try:
            await fee_service.add_rule(
                custom_plan_id, min_amount=Decimal("100"),
                max_amount=Decimal("100"),
            )
            _ok("max<=min rejected", False)
        except ValidationError:
            _ok("max<=min rejected", True)

        # 15. duplicate plan code → ConflictError
        try:
            await fee_service.create_plan(code=plan_code, name="dup")
            _ok("duplicate plan code rejected", False)
        except ConflictError:
            _ok("duplicate plan code rejected", True)

        # 16. preview wrapper does NOT record
        comps_before = await fee_service.list_computations(
            merchant_id=OTHER_MID, limit=500
        )
        await fee_service.preview_fee(
            OTHER_MID, gross=Decimal("100"), payment_method="upi"
        )
        comps_after = await fee_service.list_computations(
            merchant_id=OTHER_MID, limit=500
        )
        _ok("preview does not record",
            len(comps_after) == len(comps_before),
            f"before={len(comps_before)} after={len(comps_after)}")

        print("\nALL PHASE-10 SMOKE PASSED")
    finally:
        # cleanup: must disable trigger to delete computations
        async with get_connection() as conn:
            await conn.execute(
                "ALTER TABLE fee_computations DISABLE TRIGGER trg_fee_comp_no_delete"
            )
            try:
                await conn.execute(
                    "DELETE FROM fee_computations WHERE merchant_id = $1::uuid",
                    OTHER_MID,
                )
                await conn.execute(
                    "DELETE FROM merchant_fee_overrides WHERE merchant_id = $1::uuid",
                    OTHER_MID,
                )
                if custom_plan_id:
                    await conn.execute(
                        "DELETE FROM fee_plan_rules WHERE plan_id = $1",
                        custom_plan_id,
                    )
                    await conn.execute(
                        "DELETE FROM fee_plans WHERE id = $1",
                        custom_plan_id,
                    )
            finally:
                await conn.execute(
                    "ALTER TABLE fee_computations ENABLE TRIGGER trg_fee_comp_no_delete"
                )
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
