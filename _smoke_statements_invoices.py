"""
Smoke test for Phase 5 — Statements & (Tax) Invoices.

Exercises the full lifecycle WITHOUT touching any payment gateway:

   create_draft → add_line(CGST+SGST) → add_line(IGST) → recompute totals
   → issue (auto-allocates INV-{FY}-{M4}-NNNNN) → to_csv (assertions)
   create_draft → cancel(reason)
   issued → cannot add line (ValidationError)
   issue with zero lines → ValidationError

Statements:
   generate(period 90d back → now) → assert opening/credits/debits/closing
   list/get/list_entries/to_csv (assertions)
   period_end <= period_start → ValidationError
   merchant scope: list_invoices for unrelated UUID returns []

Run with active venv:
    venv\\Scripts\\python.exe _smoke_statements_invoices.py
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from app.core.database import (
    close_db_pool, get_connection, get_transaction, init_db_pool,
)
from app.core.exceptions import ValidationError
from app.services.merchant_statement_service import merchant_statement_service
from app.services.tax_invoice_service import tax_invoice_service

MERCHANT_ID = "751c6d1d-1559-45f2-a24b-7ecd16678113"
USER_ID     = "a07da9d2-1235-4af5-bcd3-9ba56b6edc47"
OTHER_MERCHANT_ID = str(uuid4())


def section(name: str) -> None:
    print(f"\n── {name} " + "─" * (60 - len(name)))


async def cleanup() -> None:
    print("[cleanup] removing prior smoke artefacts…")
    async with get_transaction() as cx:
        # tax_invoice_line_items will cascade
        await cx.execute(
            "DELETE FROM tax_invoices "
            "WHERE merchant_id=$1::uuid AND notes LIKE 'smoke:%'",
            MERCHANT_ID,
        )
        await cx.execute(
            "DELETE FROM merchant_statements "
            "WHERE merchant_id=$1::uuid AND notes LIKE 'smoke:%'",
            MERCHANT_ID,
        )


async def main() -> None:
    await init_db_pool()
    try:
        print(f"\n=== Phase 5 statements & invoices smoke ===\nmerchant={MERCHANT_ID}\n")
        await cleanup()

        # ════════════════════════════════════════════════════════════════
        # INVOICES
        # ════════════════════════════════════════════════════════════════
        section("create_draft")
        inv = await tax_invoice_service.create_draft(
            merchant_id=MERCHANT_ID,
            currency="INR",
            place_of_supply="29-Karnataka",
            gstin_supplier="29AABCB1234F1Z5",
            gstin_customer="29AABCC9876D1Z3",
            supplier_name="Bittu Technologies Pvt Ltd",
            supplier_address="Bengaluru, KA",
            customer_name="Smoke Merchant",
            customer_address="Bengaluru, KA",
            notes="smoke: full lifecycle",
            metadata={"smoke": True},
            created_by=USER_ID,
        )
        assert inv["status"] == "draft", inv
        assert inv["invoice_number"].startswith("DRAFT-"), inv["invoice_number"]
        assert float(inv["total_amount"]) == 0.0
        print(f"  draft_id={inv['id']} placeholder_number={inv['invoice_number']}")
        invoice_id = inv["id"]

        section("add_line CGST+SGST")
        l1 = await tax_invoice_service.add_line(
            invoice_id=invoice_id,
            description="Platform fee — May 2025",
            hsn_sac="998314",
            quantity=1, unit_amount=1000.0, discount_amount=0,
            cgst_rate=9.0, sgst_rate=9.0,
        )
        # taxable=1000, cgst=90, sgst=90, line_total=1180
        assert abs(l1["taxable_amount"] - 1000.0) < 0.01, l1
        assert abs(l1["cgst_amount"] - 90.0) < 0.01, l1
        assert abs(l1["sgst_amount"] - 90.0) < 0.01, l1
        assert abs(l1["line_total"] - 1180.0) < 0.01, l1
        print(f"  line1 sno={l1['sno']} taxable={l1['taxable_amount']} total={l1['line_total']}")

        section("add_line IGST (interstate)")
        l2 = await tax_invoice_service.add_line(
            invoice_id=invoice_id,
            description="Subscription — May 2025",
            hsn_sac="998314",
            quantity=2, unit_amount=500.0, discount_amount=100.0,
            igst_rate=18.0,
        )
        # taxable = 2*500-100 = 900; igst = 162; line_total = 1062
        assert abs(l2["taxable_amount"] - 900.0) < 0.01, l2
        assert abs(l2["igst_amount"] - 162.0) < 0.01, l2
        assert abs(l2["line_total"] - 1062.0) < 0.01, l2
        print(f"  line2 sno={l2['sno']} taxable={l2['taxable_amount']} total={l2['line_total']}")

        section("verify recomputed header totals")
        inv2 = await tax_invoice_service.get_invoice(
            invoice_id=invoice_id, merchant_id=MERCHANT_ID,
        )
        assert abs(inv2["subtotal"] - 1900.0) < 0.01, inv2
        assert abs(inv2["cgst_total"] - 90.0) < 0.01, inv2
        assert abs(inv2["sgst_total"] - 90.0) < 0.01, inv2
        assert abs(inv2["igst_total"] - 162.0) < 0.01, inv2
        assert abs(inv2["total_amount"] - 2242.0) < 0.01, inv2
        assert len(inv2["line_items"]) == 2
        print(f"  subtotal={inv2['subtotal']} total={inv2['total_amount']}")

        section("validation: IGST + CGST mutually exclusive")
        try:
            await tax_invoice_service.add_line(
                invoice_id=invoice_id, description="bad",
                quantity=1, unit_amount=100.0,
                cgst_rate=9.0, igst_rate=18.0,
            )
        except ValidationError as e:
            print(f"  rejected: {e}")
        else:
            raise AssertionError("expected ValidationError for igst+cgst")

        section("validation: discount exceeds qty*unit")
        try:
            await tax_invoice_service.add_line(
                invoice_id=invoice_id, description="bad",
                quantity=1, unit_amount=100.0, discount_amount=200.0,
            )
        except ValidationError as e:
            print(f"  rejected: {e}")
        else:
            raise AssertionError("expected ValidationError for over-discount")

        section("issue → assigns INV-{FY}-{M4}-NNNNN")
        issued = await tax_invoice_service.issue(
            invoice_id=invoice_id, actor_id=USER_ID,
        )
        assert issued["status"] == "issued"
        num = issued["invoice_number"]
        parts = num.split("-")
        assert parts[0] == "INV" and len(parts) == 4, num
        assert len(parts[1]) == 4 and parts[1].isdigit(), parts  # FY
        assert len(parts[2]) == 4, parts  # merchant prefix
        assert parts[3].isdigit() and len(parts[3]) >= 5, parts  # seq
        print(f"  issued number={num}")

        section("cannot add lines after issue")
        try:
            await tax_invoice_service.add_line(
                invoice_id=invoice_id, description="late line",
                quantity=1, unit_amount=10.0,
            )
        except ValidationError as e:
            print(f"  rejected: {e}")
        else:
            raise AssertionError("expected ValidationError when adding to issued invoice")

        section("to_csv")
        csv_out = await tax_invoice_service.to_csv(
            invoice_id=invoice_id, merchant_id=MERCHANT_ID,
        )
        assert csv_out["file_name"] == f"{num}.csv"
        body = csv_out["file_content"]
        assert "Invoice" in body and num in body
        assert "Platform fee" in body and "Subscription" in body
        assert "Total" in body and "2242" in body
        print(f"  csv {len(body)} bytes — header+totals present")

        section("create_draft → cancel(reason)")
        d2 = await tax_invoice_service.create_draft(
            merchant_id=MERCHANT_ID, currency="INR",
            notes="smoke: cancel-path", metadata={"smoke": True},
            created_by=USER_ID,
        )
        try:
            await tax_invoice_service.cancel(
                invoice_id=d2["id"], actor_id=USER_ID, reason="x",
            )
        except ValidationError:
            print("  rejected reason<3 chars ok")
        else:
            raise AssertionError("expected ValidationError for short reason")
        c2 = await tax_invoice_service.cancel(
            invoice_id=d2["id"], actor_id=USER_ID, reason="cancelled by smoke",
        )
        assert c2["status"] == "cancelled"
        print(f"  cancelled ok: {c2['cancellation_reason']}")

        section("issue zero-line draft → rejected")
        d3 = await tax_invoice_service.create_draft(
            merchant_id=MERCHANT_ID, currency="INR",
            notes="smoke: zero-lines", metadata={"smoke": True},
            created_by=USER_ID,
        )
        try:
            await tax_invoice_service.issue(invoice_id=d3["id"], actor_id=USER_ID)
        except ValidationError as e:
            print(f"  rejected: {e}")
        else:
            raise AssertionError("expected ValidationError for zero-line issue")

        section("merchant scope: other-merchant cannot see")
        other = await tax_invoice_service.list_invoices(
            merchant_id=OTHER_MERCHANT_ID, limit=10,
        )
        assert other == [], f"other merchant should not see invoices: {len(other)}"
        print(f"  other merchant sees {len(other)} invoices")

        # ════════════════════════════════════════════════════════════════
        # STATEMENTS
        # ════════════════════════════════════════════════════════════════
        section("statement.generate (last 90 days)")
        now = datetime.now(timezone.utc)
        ps = now - timedelta(days=90)
        st = await merchant_statement_service.generate(
            merchant_id=MERCHANT_ID,
            period_start=ps, period_end=now,
            currency="INR",
            generated_by=USER_ID,
            notes="smoke: 90d window",
            metadata={"smoke": True},
        )
        assert st["status"] == "ready"
        assert st["currency"] == "INR"
        print(f"  statement_id={st['id']} txn_count={st['txn_count']} "
              f"opening={st['opening_balance']} credits={st['total_credits']} "
              f"debits={st['total_debits']} closing={st['closing_balance']}")
        # Math sanity: closing == opening + credits - debits (within 0.01)
        diff = abs((st["opening_balance"] + st["total_credits"]
                    - st["total_debits"]) - st["closing_balance"])
        assert diff < 0.01, f"balance math drift={diff}"
        print(f"  balance equation holds (drift={diff:.6f})")
        statement_id = st["id"]

        section("get + list_entries")
        st2 = await merchant_statement_service.get_statement(
            statement_id=statement_id, merchant_id=MERCHANT_ID,
        )
        assert st2["id"] == statement_id
        entries = await merchant_statement_service.list_entries(
            statement_id=statement_id, merchant_id=MERCHANT_ID, limit=2000,
        )
        assert len(entries) == st["txn_count"], (
            f"entries count {len(entries)} != txn_count {st['txn_count']}"
        )
        # Verify credit/debit sums match header (within rounding tolerance)
        sum_cr = sum(e["credit_amount"] or 0 for e in entries)
        sum_db = sum(e["debit_amount"]  or 0 for e in entries)
        assert abs(sum_cr - st["total_credits"]) < 0.01, (sum_cr, st["total_credits"])
        assert abs(sum_db - st["total_debits"])  < 0.01, (sum_db, st["total_debits"])
        print(f"  entries={len(entries)} sum_cr={sum_cr} sum_db={sum_db}")

        section("to_csv")
        csv2 = await merchant_statement_service.to_csv(
            statement_id=statement_id, merchant_id=MERCHANT_ID,
        )
        body2 = csv2["file_content"]
        assert "Merchant Statement" in body2
        assert "Opening Balance" in body2
        assert "Closing Balance" in body2
        print(f"  csv {len(body2)} bytes — header present")

        section("validation: period_end <= period_start")
        try:
            await merchant_statement_service.generate(
                merchant_id=MERCHANT_ID,
                period_start=now, period_end=now,
                generated_by=USER_ID, notes="smoke: bad",
            )
        except ValidationError as e:
            print(f"  rejected: {e}")
        else:
            raise AssertionError("expected ValidationError for inverted window")

        section("merchant scope: other-merchant statement listing")
        others = await merchant_statement_service.list_statements(
            merchant_id=OTHER_MERCHANT_ID, limit=10,
        )
        assert others == [], f"other merchant should not see statements: {len(others)}"
        print(f"  other merchant sees {len(others)} statements")

        section("admin can cancel statement")
        cnc = await merchant_statement_service.cancel(
            statement_id=statement_id, actor_id=USER_ID,
            reason="cancelled by smoke",
        )
        assert cnc["status"] == "cancelled"
        print(f"  cancelled ok: {cnc['cancellation_reason']}")

        print("\n=== Phase 5 smoke OK ===\n")
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
