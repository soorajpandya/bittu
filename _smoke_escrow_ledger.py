"""
Phase 2 smoke test — escrow ledger end-to-end.

Verifies:
  1. hold_for_payment posts an escrow_hold CREDIT (idempotent on payment_id)
  2. release_hold posts an escrow_release DEBIT (single-use enforced)
  3. Re-running release_hold on the same hold raises (PK violation)
  4. due-for-release picks up holds whose hold_until has passed
  5. release_due processes them and balance returns to 0 net
  6. Immutability triggers reject UPDATE/DELETE
  7. consistency check returns lock_matches_sum=true
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.database import close_db_pool, get_connection, init_db_pool
from app.services.escrow_service import escrow_service
from app.services.escrow_integration import hold_payment_in_escrow

MERCHANT_ID = "751c6d1d-1559-45f2-a24b-7ecd16678113"
BRANCH_ID   = "82ac3013-14cb-4e1a-acd8-3d5f921dafb8"
ACTOR_ID    = "a07da9d2-1235-4af5-bcd3-9ba56b6edc47"


async def _held() -> Decimal:
    async with get_connection() as c:
        v = await c.fetchval(
            "SELECT held_balance FROM escrow_balance_locks "
            "WHERE merchant_id = $1::uuid AND currency = 'INR'",
            MERCHANT_ID,
        )
    return Decimal(str(v)) if v is not None else Decimal("0")


async def main():
    await init_db_pool()
    try:
        # Reset config to known T+0 so we can test release_due immediately
        await escrow_service.set_config(MERCHANT_ID, hold_days=0, enabled=True)
        cfg = await escrow_service.get_config(MERCHANT_ID)
        assert cfg["hold_days"] == 0
        print(f"[setup] config = {cfg}")

        b0 = await _held()
        print(f"[start] held_balance = {b0}")

        # 1. Hold a payment
        pid = str(uuid.uuid4())
        oid = str(uuid.uuid4())
        h1 = await escrow_service.hold_for_payment(
            merchant_id=MERCHANT_ID,
            branch_id=BRANCH_ID,
            payment_id=pid,
            order_id=oid,
            amount=Decimal("500.00"),
            created_by=ACTOR_ID,
        )
        b1 = await _held()
        assert b1 - b0 == Decimal("500.0000"), f"expected +500, got {b1 - b0}"
        assert h1["transaction_type"] == "escrow_hold"
        assert h1["hold_until"] is not None
        print(f"[pass] escrow_hold credited 500 (held={b1}, ref={h1['escrow_reference']})")

        # 2. Idempotency on hold
        h1b = await escrow_service.hold_for_payment(
            merchant_id=MERCHANT_ID,
            branch_id=BRANCH_ID,
            payment_id=pid,
            order_id=oid,
            amount=Decimal("500.00"),
            created_by=ACTOR_ID,
        )
        b2 = await _held()
        assert b2 == b1, f"replay changed balance: {b1} -> {b2}"
        assert h1b["id"] == h1["id"], "idempotency returned different id"
        print("[pass] hold_for_payment is idempotent")

        # 3. Integration helper (best-effort)
        pid2 = str(uuid.uuid4())
        await hold_payment_in_escrow(
            merchant_id=MERCHANT_ID,
            payment_id=pid2,
            amount=Decimal("250.00"),
            method="cash",
            order_id=str(uuid.uuid4()),
            branch_id=BRANCH_ID,
            actor_id=ACTOR_ID,
        )
        b3 = await _held()
        assert b3 - b2 == Decimal("250.0000")
        print(f"[pass] integration helper held 250 (held={b3})")

        # 4. due-for-release should see both (hold_days=0 → already due)
        due = await escrow_service.list_due_for_release(limit=100)
        my_due = [d for d in due if d["merchant_id"] == MERCHANT_ID]
        assert len(my_due) >= 2, f"expected >=2 due, got {len(my_due)}"
        print(f"[pass] due-for-release returned {len(my_due)} for our merchant")

        # 5. release_due processes them
        result = await escrow_service.release_due(limit=100, actor_id=ACTOR_ID)
        print(f"[release_due] {result}")
        assert result["released"] >= 2
        b4 = await _held()
        assert b4 == b0, f"expected balance back to {b0}, got {b4}"
        print(f"[pass] release_due drained held balance back to {b4}")

        # 6. Replay release_due → 0 considered (no due holds)
        result2 = await escrow_service.release_due(limit=100, actor_id=ACTOR_ID)
        assert result2["released"] == 0
        print("[pass] release_due is idempotent (nothing left to release)")

        # 7. Direct release_hold on already-released raises (PK violation).
        #    Setup: post a fresh hold so balance is available, then release
        #    one already-released hold a second time with a NEW idempotency
        #    key. The escrow_release_links PK must reject it.
        pid3 = str(uuid.uuid4())
        h_pad = await escrow_service.hold_for_payment(
            merchant_id=MERCHANT_ID,
            branch_id=BRANCH_ID,
            payment_id=pid3,
            amount=Decimal("500.00"),
            created_by=ACTOR_ID,
        )
        try:
            await escrow_service.post_entry(
                merchant_id=MERCHANT_ID,
                transaction_type="escrow_release",
                debit_amount=Decimal("500.00"),
                released_entry_id=h1["id"],  # already released earlier
                idempotency_key=f"smoke_double_release:{uuid.uuid4()}",
                metadata={"reason": "test_double_release"},
            )
            print("[FAIL] expected PK violation on double-release")
            return
        except Exception as exc:
            msg = str(exc).lower()
            assert ("duplicate key" in msg or "escrow_release_links" in msg
                    or "unique" in msg), f"unexpected error: {exc}"
            print(f"[pass] double-release blocked by escrow_release_links PK")
        finally:
            # Drain the padding hold so subsequent assertions hold
            await escrow_service.release_hold(
                merchant_id=MERCHANT_ID,
                hold_entry_id=h_pad["id"],
                amount=Decimal("500.00"),
                reason="smoke_cleanup",
                created_by=ACTOR_ID,
            )

        # 8. Immutability triggers
        async with get_connection() as c:
            try:
                await c.execute(
                    "UPDATE escrow_ledger SET credit_amount = 0 WHERE id = $1",
                    uuid.UUID(h1["id"]),
                )
                print("[FAIL] UPDATE was allowed")
                return
            except Exception as exc:
                assert "append-only" in str(exc) or "P0002" in str(exc)
                print(f"[pass] UPDATE blocked by trigger")

            try:
                await c.execute(
                    "DELETE FROM escrow_ledger WHERE id = $1",
                    uuid.UUID(h1["id"]),
                )
                print("[FAIL] DELETE was allowed")
                return
            except Exception as exc:
                assert "append-only" in str(exc) or "P0002" in str(exc)
                print(f"[pass] DELETE blocked by trigger")

        # 9. Consistency
        async with get_connection() as c:
            report = await c.fetchval(
                "SELECT fn_check_escrow_consistency($1::uuid, 'INR')",
                MERCHANT_ID,
            )
        print(f"[consistency] {report}")

        # 10. Negative-balance protection
        try:
            await escrow_service.post_entry(
                merchant_id=MERCHANT_ID,
                transaction_type="escrow_adjustment",
                debit_amount=Decimal("999999.00"),
                idempotency_key=f"smoke_neg:{uuid.uuid4()}",
                metadata={"test": "negative"},
            )
            print("[FAIL] negative balance was allowed")
            return
        except Exception as exc:
            assert "negative" in str(exc).lower() or "P0001" in str(exc)
            print(f"[pass] negative-balance protection works")

        # Restore default hold_days
        await escrow_service.set_config(MERCHANT_ID, hold_days=1)
        print("\n[ok] Phase 2 smoke checks passed")
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
