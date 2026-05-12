"""
Smoke test for Phase 4 — Payouts / Disbursement Engine.

Exercises the full lifecycle WITHOUT touching any payment gateway:

   beneficiary    → request → cancel
   beneficiary    → request → approve → batch → generate-file → mark-sent → mark-completed
   beneficiary    → request → approve → batch → generate-file → mark-sent → mark-failed (reverses)
   merchant scope assertion (other-merchant cannot see this merchant's payouts)

Run with active venv:
    venv\\Scripts\\python.exe _smoke_payouts.py
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from uuid import uuid4

from app.core.database import get_connection, get_transaction, init_db_pool, close_db_pool
from app.services.payout_service import payout_service

MERCHANT_ID = "751c6d1d-1559-45f2-a24b-7ecd16678113"
USER_ID     = "a07da9d2-1235-4af5-bcd3-9ba56b6edc47"
OTHER_MERCHANT_ID = str(uuid4())  # random uuid that won't match anything

LABEL = "smoke-bene"
SEED_AMOUNT = Decimal("100000")  # ₹1,00,000 to fund the merchant ledger if needed


async def cleanup() -> None:
    print("[cleanup] removing prior smoke artefacts…")
    async with get_transaction() as cx:
        # Delete batches that contain only smoke payouts
        await cx.execute("""
            DELETE FROM payout_status_events
             WHERE payout_id IN (
                SELECT id FROM payout_requests
                 WHERE merchant_id=$1::uuid AND notes LIKE 'smoke:%'
             )
        """, MERCHANT_ID)
        # Capture batch_ids to maybe clean
        batch_rows = await cx.fetch("""
            SELECT DISTINCT batch_id FROM payout_requests
             WHERE merchant_id=$1::uuid AND notes LIKE 'smoke:%' AND batch_id IS NOT NULL
        """, MERCHANT_ID)
        await cx.execute(
            "DELETE FROM payout_requests WHERE merchant_id=$1::uuid AND notes LIKE 'smoke:%'",
            MERCHANT_ID,
        )
        for br in batch_rows:
            await cx.execute("DELETE FROM payout_batches WHERE id=$1::uuid",
                             str(br["batch_id"]))
        await cx.execute(
            "DELETE FROM payout_beneficiaries WHERE merchant_id=$1::uuid AND label=$2",
            MERCHANT_ID, LABEL,
        )


async def ensure_funds() -> None:
    """Make sure the merchant has enough ledger balance for smoke payouts."""
    async with get_connection() as c:
        bal = await c.fetchval(
            "SELECT COALESCE(current_balance, 0) FROM merchant_ledger_balance_locks "
            "WHERE merchant_id=$1::uuid AND currency='INR'",
            MERCHANT_ID,
        )
    bal = Decimal(str(bal or 0))
    print(f"[funds] current ledger balance = {bal}")
    if bal >= SEED_AMOUNT:
        return
    topup = SEED_AMOUNT - bal
    print(f"[funds] topping up {topup} via fn_post_merchant_ledger_entry (manual_credit)…")
    async with get_transaction() as cx:
        await cx.fetchval(
            """
            SELECT fn_post_merchant_ledger_entry(
                $1::uuid, NULL, 'manual_credit'::merchant_ledger_txn_type,
                0, $2, 'INR',
                'smoke', NULL, NULL, NULL, NULL,
                NULL, NULL, $3, $4::jsonb, $5::uuid
            )
            """,
            MERCHANT_ID, topup,
            f"smoke-fund-{uuid4().hex[:8]}",
            json.dumps({"smoke": True}), USER_ID,
        )


def section(name: str) -> None:
    print(f"\n── {name} " + "─" * (60 - len(name)))


async def main() -> None:
    await init_db_pool()
    try:
        print(f"\n=== Phase 4 payouts smoke ===\nmerchant={MERCHANT_ID}\n")
        await cleanup()
        await ensure_funds()

        # ── 1. Beneficiary
        section("create_beneficiary")
        ben = await payout_service.create_beneficiary(
            merchant_id=MERCHANT_ID, label=LABEL, type="bank",
            account_holder="Smoke Test Co",
            account_number="1234567890",
            ifsc="HDFC0001234", bank_name="HDFC Bank",
            created_by=USER_ID,
        )
        assert ben["account_number_last4"] == "7890", ben
        print(f"  beneficiary_id={ben['id']} last4={ben['account_number_last4']}")
        # idempotent UPSERT
        ben2 = await payout_service.create_beneficiary(
            merchant_id=MERCHANT_ID, label=LABEL, type="bank",
            account_holder="Smoke Test Co",
            account_number="9999987654",  # different account — UPSERT should update
            ifsc="HDFC0001234", bank_name="HDFC Bank",
            created_by=USER_ID,
        )
        assert ben2["id"] == ben["id"], "UPSERT should keep same id"
        assert ben2["account_number_last4"] == "7654"
        print("  upsert ok (id stable, last4 updated)")
        beneficiary_id = ben["id"]

        # ── 2. Available balance
        section("available_balance")
        bal = await payout_service.available_balance(merchant_id=MERCHANT_ID)
        print(f"  current={bal['current_balance']} locked={bal['in_flight_locked']} "
              f"available={bal['available_balance']}")
        assert bal["available_balance"] >= 100, bal

        # ── 3. Request payout (cancel flow)
        section("request → cancel")
        idem = f"smoke-{uuid4().hex[:8]}"
        p_cancel = await payout_service.request_payout(
            merchant_id=MERCHANT_ID, beneficiary_id=beneficiary_id,
            amount=100, method="bank_neft", requested_by=USER_ID,
            notes="smoke:cancel", idempotency_key=idem,
        )
        print(f"  ref={p_cancel['payout_reference']} status={p_cancel['status']}")
        assert p_cancel["status"] == "requested"

        # idempotency replay
        replay = await payout_service.request_payout(
            merchant_id=MERCHANT_ID, beneficiary_id=beneficiary_id,
            amount=100, method="bank_neft", requested_by=USER_ID,
            notes="smoke:cancel", idempotency_key=idem,
        )
        assert replay["id"] == p_cancel["id"], "idempotent replay should return same row"
        print("  idempotency replay ok")

        # available dropped
        bal2 = await payout_service.available_balance(merchant_id=MERCHANT_ID)
        assert bal2["in_flight_locked"] >= 100, bal2
        print(f"  in_flight_locked now {bal2['in_flight_locked']}")

        # over-budget should fail
        try:
            await payout_service.request_payout(
                merchant_id=MERCHANT_ID, beneficiary_id=beneficiary_id,
                amount=99_999_999, method="bank_neft", requested_by=USER_ID,
                notes="smoke:overbudget",
            )
            raise AssertionError("over-budget request should have failed")
        except Exception as e:
            assert "insufficient" in str(e).lower(), e
            print(f"  over-budget rejected ✓ ({e})")

        cancelled = await payout_service.cancel_payout(
            payout_id=p_cancel["id"], merchant_id=MERCHANT_ID,
            actor_id=USER_ID, notes="smoke:cancel-test",
        )
        assert cancelled["status"] == "cancelled"
        print("  cancelled ✓")

        # cancel non-requested should fail
        try:
            await payout_service.cancel_payout(
                payout_id=p_cancel["id"], merchant_id=MERCHANT_ID,
                actor_id=USER_ID,
            )
            raise AssertionError("cancel of cancelled payout should have failed")
        except Exception as e:
            print(f"  re-cancel rejected ✓ ({type(e).__name__})")

        # ── 4. Happy path: request → approve → batch → file → sent → completed
        section("happy path → completed")
        bal_before = (await payout_service.available_balance(merchant_id=MERCHANT_ID))["current_balance"]

        p_ok = await payout_service.request_payout(
            merchant_id=MERCHANT_ID, beneficiary_id=beneficiary_id,
            amount=250, method="bank_neft", requested_by=USER_ID,
            notes="smoke:happy",
        )
        approved = await payout_service.approve_payout(
            payout_id=p_ok["id"], actor_id=USER_ID, notes="smoke:approve",
        )
        assert approved["status"] == "approved"
        print(f"  approved {p_ok['payout_reference']}")

        batch = await payout_service.create_batch(
            actor_id=USER_ID, merchant_id=MERCHANT_ID,
            payout_ids=[p_ok["id"]], currency="INR", notes="smoke:batch",
        )
        print(f"  batch={batch['batch_reference']} count={batch['total_count']} total={batch['total_amount']}")

        # Generate file
        gen = await payout_service.generate_batch_file(
            batch_id=batch["id"], actor_id=USER_ID, file_format="neft_csv",
        )
        assert gen["row_count"] == 1
        first_line = gen["file_content"].splitlines()[0]
        assert "payout_reference" in first_line
        print(f"  file rows={gen['row_count']} header={first_line[:60]}…")

        sent = await payout_service.mark_sent(
            payout_id=p_ok["id"], actor_id=USER_ID,
            utr_number="UTR-SMOKE-001", bank_reference="BANK-REF-001",
            notes="smoke:mark-sent",
        )
        assert sent["status"] == "sent"
        assert sent["ledger_entry_id"], "ledger_entry_id should be set"
        bal_after_sent = (await payout_service.available_balance(merchant_id=MERCHANT_ID))["current_balance"]
        assert bal_after_sent <= bal_before - 250 + 0.01, \
            f"balance should drop by 250: before={bal_before} after={bal_after_sent}"
        print(f"  sent ✓ ledger={sent['ledger_entry_id']} bal: {bal_before} → {bal_after_sent}")

        completed = await payout_service.mark_completed(
            payout_id=p_ok["id"], actor_id=USER_ID, notes="smoke:complete",
        )
        assert completed["status"] == "completed"
        print("  completed ✓")

        # ── 5. Failure path: request → approve → batch → file → sent → failed (reverses)
        section("failure path → reversed")
        bal_pre = (await payout_service.available_balance(merchant_id=MERCHANT_ID))["current_balance"]

        p_fail = await payout_service.request_payout(
            merchant_id=MERCHANT_ID, beneficiary_id=beneficiary_id,
            amount=175, method="bank_neft", requested_by=USER_ID,
            notes="smoke:fail",
        )
        await payout_service.approve_payout(payout_id=p_fail["id"], actor_id=USER_ID)
        b2 = await payout_service.create_batch(
            actor_id=USER_ID, merchant_id=MERCHANT_ID,
            payout_ids=[p_fail["id"]], notes="smoke:batch-fail",
        )
        await payout_service.generate_batch_file(
            batch_id=b2["id"], actor_id=USER_ID, file_format="neft_csv",
        )
        sent2 = await payout_service.mark_sent(
            payout_id=p_fail["id"], actor_id=USER_ID,
            utr_number="UTR-SMOKE-FAIL",
        )
        bal_after_sent = (await payout_service.available_balance(merchant_id=MERCHANT_ID))["current_balance"]
        failed = await payout_service.mark_failed(
            payout_id=p_fail["id"], actor_id=USER_ID,
            reason="bank declined NEFT", notes="smoke:fail",
        )
        assert failed["status"] == "failed"
        assert failed["reversal_entry_id"], "reversal_entry_id should be set"
        bal_after_fail = (await payout_service.available_balance(merchant_id=MERCHANT_ID))["current_balance"]
        assert abs(bal_after_fail - bal_pre) < 0.01, \
            f"balance should restore: pre={bal_pre} after_fail={bal_after_fail}"
        print(f"  reversed ✓ rev_id={failed['reversal_entry_id']}")
        print(f"  bal: pre={bal_pre} sent={bal_after_sent} after_fail={bal_after_fail}")

        # ── 6. Merchant scope isolation
        section("merchant scope isolation")
        mine = await payout_service.list_payouts(merchant_id=MERCHANT_ID, limit=200)
        other = await payout_service.list_payouts(merchant_id=OTHER_MERCHANT_ID, limit=200)
        assert len(other) == 0, "other merchant must not see this merchant's payouts"
        print(f"  mine={len(mine)} other={len(other)} ✓")

        # ── 7. Summaries
        section("summaries")
        s = await payout_service.get_summary(merchant_id=MERCHANT_ID)
        print(f"  by_status={s['by_status']}")
        print(f"  totals={s['totals']}")

        glob = await payout_service.get_summary()
        print(f"  global all_count={glob['totals']['all_count']}")

        by_m = await payout_service.admin_summary_by_merchant()
        print(f"  by-merchant rows={len(by_m)}")
        assert any(r["merchant_id"] == MERCHANT_ID for r in by_m)

        # ── 8. Events trail
        section("events trail")
        evs = await payout_service.list_events(p_fail["id"])
        types = [e["event_type"] for e in evs]
        print(f"  fail-payout events: {types}")
        assert "created" in types and "approved" in types and "batched" in types
        assert "sent" in types and "failed" in types and "reversed" in types

        print("\n✅ Phase 4 smoke PASSED")
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
