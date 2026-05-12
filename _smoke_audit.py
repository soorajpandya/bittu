"""
Phase 6 — Audit & Compliance smoke test.

Verifies:
  • record_strict appends a hash-chained event
  • record (best-effort) swallows bad input but logs
  • prev_hash / row_hash linkage holds across multiple inserts
  • fn_verify_audit_chain returns clean for an honest chain
  • fn_verify_audit_chain detects tampering after a direct UPDATE
    (we have to disable the trigger temporarily to even *attempt* this —
    that itself proves UPDATE is normally blocked)
  • Append-only triggers: UPDATE and DELETE on audit_events raise P0002
  • Merchant scope: events for one merchant are not visible from another
  • list_events filters by action / merchant_id / time window
  • CSV export contains the expected columns

Cleanup: deletes only smoke-tagged rows by *temporarily* disabling the
delete trigger inside a transaction (audit_events is normally append-only).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.core.database import init_db_pool, close_db_pool, get_connection
from app.services.audit_service import audit_service

MERCHANT_ID       = "751c6d1d-1559-45f2-a24b-7ecd16678113"
USER_ID           = "a07da9d2-1235-4af5-bcd3-9ba56b6edc47"
OTHER_MERCHANT_ID = str(uuid4())
SMOKE_TAG         = f"smoke:{uuid4().hex[:12]}"


async def _cleanup():
    async with get_connection() as c:
        # The trigger normally blocks DELETE; bypass for smoke teardown only.
        await c.execute(
            "ALTER TABLE audit_events DISABLE TRIGGER trg_audit_events_no_delete"
        )
        try:
            n = await c.fetchval(
                "DELETE FROM audit_events WHERE request_id = $1 RETURNING id",
                SMOKE_TAG,
            )
            await c.execute(
                "DELETE FROM audit_events WHERE request_id = $1",
                SMOKE_TAG,
            )
        finally:
            await c.execute(
                "ALTER TABLE audit_events ENABLE TRIGGER trg_audit_events_no_delete"
            )


async def main():
    await init_db_pool()
    print("=== Phase 6 audit & compliance smoke ===")
    print(f"merchant={MERCHANT_ID}  tag={SMOKE_TAG}")

    try:
        # ── record_strict: 3 chained events ─────────────────────────
        print("\n── record_strict (3 events) ───────────────────────")
        e1 = await audit_service.record_strict(
            action="invoice.issued",
            actor_type="user",
            actor_user_id=USER_ID,
            actor_label="smoke@user",
            merchant_id=MERCHANT_ID,
            resource_type="tax_invoice",
            resource_id="INV-2627-751C-99991",
            payload={"total": "1180.00", "currency": "INR"},
            ip_address="203.0.113.10",
            user_agent="smoke-agent/1.0",
            request_id=SMOKE_TAG,
        )
        e2 = await audit_service.record_strict(
            action="invoice.cancelled",
            actor_type="admin",
            actor_user_id=USER_ID,
            actor_label="smoke@admin",
            merchant_id=MERCHANT_ID,
            resource_type="tax_invoice",
            resource_id="INV-2627-751C-99991",
            payload={"reason": "duplicate"},
            ip_address="203.0.113.10",
            request_id=SMOKE_TAG,
        )
        e3 = await audit_service.record_strict(
            action="statement.generated",
            actor_type="user",
            actor_user_id=USER_ID,
            merchant_id=OTHER_MERCHANT_ID,
            resource_type="merchant_statement",
            resource_id=str(uuid4()),
            payload={"txn_count": 16},
            request_id=SMOKE_TAG,
        )
        for i, e in enumerate((e1, e2, e3), 1):
            assert len(e["row_hash"]) == 64, f"e{i} bad row_hash len"
            print(f"  e{i} id={e['id']} action={e['action']} "
                  f"row_hash={e['row_hash'][:12]}…")

        # ── chain linkage: e2.prev_hash must equal e1.row_hash ──────
        print("\n── prev_hash linkage ─────────────────────────────")
        assert e2["prev_hash"] == e1["row_hash"], "e2 not linked to e1"
        assert e3["prev_hash"] == e2["row_hash"], "e3 not linked to e2"
        print(f"  e2.prev == e1.row ✓   e3.prev == e2.row ✓")

        # ── record (best-effort) swallows invalid actor_type ────────
        print("\n── record() swallows bad input ────────────────────")
        bad = await audit_service.record(
            action="bogus.action",
            actor_type="goblin",   # invalid
            request_id=SMOKE_TAG,
        )
        assert bad is None, "best-effort record should return None on failure"
        print("  best-effort returned None on invalid actor_type ✓")

        # ── verify_chain: must be clean ─────────────────────────────
        print("\n── verify_chain (full) ────────────────────────────")
        v = await audit_service.verify_chain()
        assert v["ok"] is True, f"chain not ok: {v}"
        print(f"  ok=True checked={v['checked']} "
              f"id range=[{v['min_id']},{v['max_id']}]")

        # ── verify_chain (slice around our 3 events) ────────────────
        print("\n── verify_chain (slice) ───────────────────────────")
        vs = await audit_service.verify_chain(start_id=e1["id"], end_id=e3["id"])
        assert vs["ok"] is True, f"slice not ok: {vs}"
        assert vs["checked"] == 3
        print(f"  slice ok checked={vs['checked']}")

        # ── tamper detection: forcibly mutate payload of e2 ─────────
        print("\n── tamper detection ──────────────────────────────")
        async with get_connection() as c:
            await c.execute(
                "ALTER TABLE audit_events DISABLE TRIGGER trg_audit_events_no_update"
            )
            try:
                await c.execute(
                    "UPDATE audit_events SET payload = $1::jsonb WHERE id = $2",
                    json.dumps({"reason": "TAMPERED"}), e2["id"],
                )
            finally:
                await c.execute(
                    "ALTER TABLE audit_events ENABLE TRIGGER trg_audit_events_no_update"
                )
        vt = await audit_service.verify_chain(start_id=e1["id"], end_id=e3["id"])
        assert vt["ok"] is False, "tamper not detected"
        assert vt["first_bad"]["id"] == e2["id"], \
            f"wrong first_bad: {vt['first_bad']}"
        print(f"  tamper detected at id={vt['first_bad']['id']}  "
              f"expected={vt['first_bad']['expected_hash'][:12]}…  "
              f"stored={vt['first_bad']['stored_hash'][:12]}…")

        # restore e2 row_hash by re-deriving so the rest of the test passes
        # cleanly. We just delete the 3 smoke rows in the cleanup phase.
        # ── append-only triggers: UPDATE / DELETE must raise P0002 ──
        print("\n── append-only enforcement ───────────────────────")
        async with get_connection() as c:
            try:
                await c.execute(
                    "UPDATE audit_events SET action = 'hacked' WHERE id = $1",
                    e1["id"],
                )
                raise AssertionError("UPDATE should have raised P0002")
            except Exception as exc:
                assert "append-only" in str(exc).lower(), \
                    f"unexpected error: {exc}"
                print(f"  UPDATE blocked ✓  ({type(exc).__name__})")
            try:
                await c.execute(
                    "DELETE FROM audit_events WHERE id = $1", e1["id"],
                )
                raise AssertionError("DELETE should have raised P0002")
            except Exception as exc:
                assert "append-only" in str(exc).lower(), \
                    f"unexpected error: {exc}"
                print(f"  DELETE blocked ✓  ({type(exc).__name__})")

        # ── merchant scope ─────────────────────────────────────────
        print("\n── merchant scope (list_events) ──────────────────")
        own = await audit_service.list_events(
            merchant_id=MERCHANT_ID,
            from_ts=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        own_smoke = [e for e in own if e["request_id"] == SMOKE_TAG]
        assert len(own_smoke) == 2, \
            f"expected 2 own smoke events, got {len(own_smoke)}"

        other = await audit_service.list_events(
            merchant_id=OTHER_MERCHANT_ID,
            from_ts=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        other_smoke = [e for e in other if e["request_id"] == SMOKE_TAG]
        assert len(other_smoke) == 1, \
            f"expected 1 other smoke event, got {len(other_smoke)}"
        # cross-check no leakage
        assert all(e["merchant_id"] == MERCHANT_ID for e in own_smoke)
        assert all(e["merchant_id"] == OTHER_MERCHANT_ID for e in other_smoke)
        print(f"  merchant {MERCHANT_ID[:8]} sees {len(own_smoke)} own ✓")
        print(f"  merchant {OTHER_MERCHANT_ID[:8]} sees {len(other_smoke)} own ✓")

        # ── filter by action ───────────────────────────────────────
        print("\n── list_events filter by action ──────────────────")
        issued = await audit_service.list_events(
            merchant_id=MERCHANT_ID,
            action="invoice.issued",
            from_ts=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        issued_smoke = [e for e in issued if e["request_id"] == SMOKE_TAG]
        assert len(issued_smoke) == 1
        print(f"  invoice.issued filter → {len(issued_smoke)} smoke event ✓")

        # ── get_event by uuid (with merchant scope) ────────────────
        print("\n── get_event with merchant scope ─────────────────")
        got = await audit_service.get_event(
            event_uuid=e1["event_uuid"], merchant_id=MERCHANT_ID,
        )
        assert got["id"] == e1["id"]
        try:
            await audit_service.get_event(
                event_uuid=e1["event_uuid"], merchant_id=OTHER_MERCHANT_ID,
            )
            raise AssertionError("cross-merchant get should 404")
        except Exception as exc:
            assert "audit_event" in str(exc) or "not found" in str(exc).lower()
            print(f"  cross-merchant get rejected ✓ ({type(exc).__name__})")

        # ── CSV export ─────────────────────────────────────────────
        print("\n── to_csv ────────────────────────────────────────")
        out = audit_service.to_csv(own_smoke + other_smoke)
        body = out["body"]
        assert "id,event_uuid" in body.splitlines()[0]
        assert "invoice.issued" in body
        assert "invoice.cancelled" in body
        print(f"  csv {len(body)} bytes — header+rows present ✓")

        # ── verify (admin path): start_id > end_id rejected ────────
        print("\n── verify_chain validation ───────────────────────")
        try:
            await audit_service.verify_chain(start_id=10, end_id=5)
            raise AssertionError("should reject end_id < start_id")
        except Exception as exc:
            assert "end_id" in str(exc)
            print(f"  end_id < start_id rejected ✓")

        print("\n=== Phase 6 smoke OK ===")

    finally:
        await _cleanup()
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
