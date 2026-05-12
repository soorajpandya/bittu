"""Smoke test for migration 037 / merchant_ledger."""
import asyncio
from app.core.database import init_db_pool, close_db_pool, get_connection
from app.services.merchant_ledger_service import merchant_ledger_service

MERCHANT_ID = "751c6d1d-1559-45f2-a24b-7ecd16678113"


async def main():
    await init_db_pool()
    try:
        # 1. Idempotent post
        idem = "smoke_test_037_v1"
        e1 = await merchant_ledger_service.post_entry(
            merchant_id=MERCHANT_ID,
            transaction_type="payment_received",
            credit_amount=100.50,
            currency="INR",
            source_type="smoke",
            idempotency_key=idem,
            metadata={"note": "first call"},
        )
        e2 = await merchant_ledger_service.post_entry(
            merchant_id=MERCHANT_ID,
            transaction_type="payment_received",
            credit_amount=100.50,
            currency="INR",
            source_type="smoke",
            idempotency_key=idem,
            metadata={"note": "second call (should dedupe)"},
        )
        assert e1["id"] == e2["id"], "idempotency failed!"
        print(f"  [OK] idempotency: same id {e1['id']}")
        print(f"  [OK] balance_after: {e1['balance_after']}")
        print(f"  [OK] reference: {e1['ledger_reference']}")

        # 2. Different entry, fee deduction
        e3 = await merchant_ledger_service.post_entry(
            merchant_id=MERCHANT_ID,
            transaction_type="fee_deduction",
            debit_amount=2.55,
            currency="INR",
            source_type="smoke",
            idempotency_key="smoke_test_037_v1_fee",
        )
        print(f"  [OK] fee debit posted: balance_after={e3['balance_after']}")

        # 3. Try to UPDATE — must fail
        try:
            async with get_connection() as c:
                await c.execute(
                    "UPDATE merchant_ledger SET credit_amount = 0 "
                    "WHERE id = $1::uuid",
                    e1["id"],
                )
            print("  [FAIL] UPDATE was allowed!")
        except Exception as ex:
            print(f"  [OK] UPDATE blocked: {type(ex).__name__}: {str(ex)[:80]}")

        # 4. Try to DELETE — must fail
        try:
            async with get_connection() as c:
                await c.execute(
                    "DELETE FROM merchant_ledger WHERE id = $1::uuid",
                    e1["id"],
                )
            print("  [FAIL] DELETE was allowed!")
        except Exception as ex:
            print(f"  [OK] DELETE blocked: {type(ex).__name__}: {str(ex)[:80]}")

        # 5. Consistency check
        async with get_connection() as c:
            rep = await c.fetchval(
                "SELECT fn_check_merchant_ledger_consistency($1::uuid, 'INR')",
                MERCHANT_ID,
            )
        import json
        rep = json.loads(rep) if isinstance(rep, str) else rep
        print(f"  [OK] consistency: lock_matches_sum={rep['lock_matches_sum']} "
              f"last_after_matches_sum={rep['last_after_matches_sum']} "
              f"entries={rep['entry_count']}")

        # 6. Reject mismatched debit+credit
        try:
            await merchant_ledger_service.post_entry(
                merchant_id=MERCHANT_ID,
                transaction_type="adjustment",
                debit_amount=10,
                credit_amount=10,
            )
            print("  [FAIL] both-nonzero accepted!")
        except Exception as ex:
            print(f"  [OK] both-nonzero rejected: {type(ex).__name__}")
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
