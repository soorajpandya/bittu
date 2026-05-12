"""
Phase 9 — Merchant KYC smoke test.

Walks the full FSM:
  draft → fail-submit (incomplete) → add details → fail-submit (no docs)
  → upload 3 required docs → fail-submit (no owner+bank) → add owner+bank
  → submit → set_under_review → reject (with reason) → resubmit-flow
  → re-submit → approve → suspend → unsuspend
Plus: doc verify/reject (admin), bank verify+set primary, merchant scoping,
audit trail, append-only audit enforcement.

Cleanup: deletes all created rows for the test merchant.
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

from app.core.database import init_db_pool, close_db_pool, get_connection
from app.core.exceptions import ValidationError, ConflictError, NotFoundError
from app.services.kyc_service import kyc_service

MERCHANT_ID = str(uuid4())  # fresh merchant — no clash with existing data
OTHER_MID   = str(uuid4())
USER_ID     = "a07da9d2-1235-4af5-bcd3-9ba56b6edc47"
ADMIN_ID    = "a07da9d2-1235-4af5-bcd3-9ba56b6edc47"  # acts as admin for FSM fns


async def _cleanup():
    async with get_connection() as c:
        await c.execute(
            "ALTER TABLE merchant_kyc_audit_events DISABLE TRIGGER trg_kyc_audit_no_delete"
        )
        try:
            for mid in (MERCHANT_ID, OTHER_MID):
                await c.execute("DELETE FROM merchant_kyc_audit_events WHERE merchant_id = $1::uuid", mid)
                await c.execute("DELETE FROM merchant_kyc_documents WHERE merchant_id = $1::uuid", mid)
                await c.execute("DELETE FROM merchant_kyc_owners WHERE merchant_id = $1::uuid", mid)
                await c.execute("DELETE FROM merchant_kyc_bank_accounts WHERE merchant_id = $1::uuid", mid)
                await c.execute("DELETE FROM merchant_kyc_profiles WHERE merchant_id = $1::uuid", mid)
        finally:
            await c.execute(
                "ALTER TABLE merchant_kyc_audit_events ENABLE TRIGGER trg_kyc_audit_no_delete"
            )


async def main():
    await init_db_pool()
    try:
        # ── 1. profile lazy-create ──────────────────────────────────────
        prof = await kyc_service.get_or_create_profile(MERCHANT_ID)
        assert prof["status"] == "draft", prof["status"]
        assert prof["version"] == 1
        print(f"  profile created  status={prof['status']} version={prof['version']}")

        # ── 2. submit must fail: profile fields incomplete ──────────────
        try:
            await kyc_service.submit(MERCHANT_ID, actor_user_id=USER_ID)
            assert False, "expected ValidationError"
        except ValidationError as e:
            assert "incomplete" in str(e), str(e)
            print(f"  submit blocked (no fields): ok")

        # ── 3. update profile fields ────────────────────────────────────
        prof = await kyc_service.update_profile(
            MERCHANT_ID,
            legal_name="Bittu Test Pvt Ltd",
            business_type="private_limited",
            pan="ABCDE1234F",
            gstin="29ABCDE1234F1Z5",
            registered_address={"line1": "1 Smoke St", "city": "Bengaluru", "pincode": "560001"},
            contact_email="ops@smoke.test",
            contact_phone="+919999999999",
        )
        assert prof["legal_name"] == "Bittu Test Pvt Ltd"
        assert prof["pan"] == "ABCDE1234F"

        # invalid PAN should reject
        try:
            await kyc_service.update_profile(MERCHANT_ID, pan="bad")
            assert False
        except ValidationError:
            print("  invalid PAN rejected: ok")

        # ── 4. submit must still fail: missing docs/owner/bank ──────────
        try:
            await kyc_service.submit(MERCHANT_ID, actor_user_id=USER_ID)
            assert False
        except ValidationError as e:
            msg = str(e)
            assert "owners" in msg or "doc:" in msg or "primary_bank" in msg
            print(f"  submit blocked (missing deps): ok")

        # ── 5. add owner + bank + 3 required docs ───────────────────────
        owner = await kyc_service.add_owner(
            MERCHANT_ID, full_name="Test Director",
            role="director", ownership_pct=51, is_signatory=True,
            pan="QWERT5678Y",
        )
        owners = await kyc_service.list_owners(MERCHANT_ID)
        assert len(owners) == 1 and owners[0]["id"] == owner["id"]

        bank = await kyc_service.add_bank_account(
            MERCHANT_ID,
            account_holder_name="Bittu Test Pvt Ltd",
            account_number="1234567890",
            ifsc="HDFC0001234",
            bank_name="HDFC Bank", branch="Indiranagar",
        )
        assert bank["is_primary"] is True   # auto-promoted as first account
        assert bank["account_number_last4"] == "7890"
        assert "account_number_hash" not in bank  # never exposed

        for dt in ("pan_card", "address_proof", "bank_proof"):
            d = await kyc_service.add_document(
                MERCHANT_ID, doc_type=dt,
                file_url=f"https://files.bittu.test/{MERCHANT_ID}/{dt}.pdf",
                mime_type="application/pdf", size_bytes=1024,
                uploaded_by_user_id=USER_ID,
            )
            assert d["status"] == "pending"
        docs = await kyc_service.list_documents(MERCHANT_ID)
        assert len(docs) == 3

        # ── 6. now submit succeeds ──────────────────────────────────────
        prof = await kyc_service.submit(MERCHANT_ID, actor_user_id=USER_ID)
        assert prof["status"] == "submitted", prof["status"]
        print(f"  submit ok       status={prof['status']} v={prof['version']}")

        # cannot resubmit while submitted
        try:
            await kyc_service.submit(MERCHANT_ID, actor_user_id=USER_ID)
            assert False
        except ValidationError as e:
            assert "cannot submit" in str(e)
            print("  re-submit blocked: ok")

        # ── 7. admin set under_review → reject ──────────────────────────
        prof = await kyc_service.set_under_review(MERCHANT_ID, admin_id=ADMIN_ID)
        assert prof["status"] == "under_review"

        try:
            await kyc_service.review(MERCHANT_ID, admin_id=ADMIN_ID, decision="reject")
            assert False, "expected ValidationError for missing reason"
        except (ValidationError, ConflictError):
            pass

        prof = await kyc_service.review(
            MERCHANT_ID, admin_id=ADMIN_ID,
            decision="reject", reason="address proof unclear",
        )
        assert prof["status"] == "rejected"
        assert prof["rejection_reason"] == "address proof unclear"
        print(f"  reject ok       reason={prof['rejection_reason']!r}")

        # ── 8. doc reject + verify (admin) ──────────────────────────────
        addr_doc = next(d for d in docs if d["doc_type"] == "address_proof")
        rd = await kyc_service.reject_document(
            addr_doc["id"], admin_id=ADMIN_ID, reason="blurry scan",
        )
        assert rd["status"] == "rejected"

        # upload a fresh address_proof to replace it
        addr2 = await kyc_service.add_document(
            MERCHANT_ID, doc_type="address_proof",
            file_url=f"https://files.bittu.test/{MERCHANT_ID}/address_v2.pdf",
            uploaded_by_user_id=USER_ID,
        )
        vd = await kyc_service.verify_document(
            addr2["id"], admin_id=ADMIN_ID,
        )
        assert vd["status"] == "verified" and vd["verified_at"] is not None
        print(f"  doc verify/reject: ok")

        # ── 9. resubmit (rejected → submitted) ──────────────────────────
        prof = await kyc_service.submit(MERCHANT_ID, actor_user_id=USER_ID)
        assert prof["status"] == "submitted"

        # ── 10. approve → suspend → unsuspend ───────────────────────────
        prof = await kyc_service.review(
            MERCHANT_ID, admin_id=ADMIN_ID, decision="approve",
        )
        assert prof["status"] == "approved" and prof["approved_at"]
        print(f"  approved        approved_at={prof['approved_at'][:19]}")

        # cannot edit profile while approved
        try:
            await kyc_service.update_profile(MERCHANT_ID, website="https://x")
            assert False
        except ConflictError:
            print("  edit-while-approved blocked: ok")

        prof = await kyc_service.suspend(
            MERCHANT_ID, admin_id=ADMIN_ID, reason="risk review",
        )
        assert prof["status"] == "suspended"
        prof = await kyc_service.unsuspend(MERCHANT_ID, admin_id=ADMIN_ID)
        assert prof["status"] == "approved"
        print("  suspend/unsuspend: ok")

        # ── 11. bank verify + set primary contention ────────────────────
        bank2 = await kyc_service.add_bank_account(
            MERCHANT_ID,
            account_holder_name="Bittu Test Pvt Ltd",
            account_number="9988776655",
            ifsc="ICIC0005678",
        )
        assert bank2["is_primary"] is False  # first one already primary

        try:
            await kyc_service.set_primary_bank(bank2["id"], merchant_id=MERCHANT_ID)
            assert False
        except ConflictError:
            print("  set-primary unverified blocked: ok")

        await kyc_service.verify_bank_account(
            bank2["id"], admin_id=ADMIN_ID,
            method="manual", reference="ops:smoke",
        )
        promoted = await kyc_service.set_primary_bank(
            bank2["id"], merchant_id=MERCHANT_ID,
        )
        assert promoted["is_primary"] is True
        # ensure exactly one primary
        async with get_connection() as c:
            n = await c.fetchval(
                "SELECT COUNT(*) FROM merchant_kyc_bank_accounts "
                "WHERE merchant_id=$1::uuid AND is_primary=true",
                MERCHANT_ID,
            )
        assert n == 1
        print("  bank verify + set-primary: ok")

        # ── 12. merchant scoping ────────────────────────────────────────
        other_docs = await kyc_service.list_documents(OTHER_MID)
        assert other_docs == []
        try:
            await kyc_service.get_profile(OTHER_MID)
            assert False
        except NotFoundError:
            print("  merchant scoping: ok")

        # ── 13. admin cross-merchant view ───────────────────────────────
        admin_docs = await kyc_service.list_documents(merchant_id=None, limit=500)
        assert any(d["merchant_id"] == MERCHANT_ID for d in admin_docs)

        # ── 14. audit trail ─────────────────────────────────────────────
        audit = await kyc_service.list_audit_events(MERCHANT_ID, limit=200)
        events = [a["event_type"] for a in audit]
        for required in (
            "profile.submitted", "profile.under_review", "profile.rejected",
            "profile.approved", "profile.suspended", "profile.unsuspended",
            "doc.uploaded", "doc.verified", "doc.rejected",
            "bank.added", "bank.verified",
        ):
            assert required in events, (required, events)
        print(f"  audit events: {len(audit)} including all expected types")

        # append-only enforcement
        async with get_connection() as c:
            try:
                await c.execute(
                    "DELETE FROM merchant_kyc_audit_events WHERE merchant_id = $1::uuid",
                    MERCHANT_ID,
                )
                assert False, "delete should have raised P0002"
            except Exception as e:
                assert "append-only" in str(e), str(e)
        print("  audit append-only enforced: ok")

        print("\n✅ Phase 9 smoke OK")

    finally:
        await _cleanup()
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
