"""Route reporting regression tests using isolated PostgreSQL, never production."""

import csv
import io
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
from fastapi import HTTPException

from app.services import escrow_integration
from app.services import merchant_statement_service as statements
from app.services import merchant_wallet_service as wallets
from app.services import statement_service as compatibility
from app.services.razorpay import ledgers
from app.services.razorpay import merchant_finance_service as finance
from app.services.razorpay.fee_policy import provisional_merchant_transfer_paise, quote_fee, reporting_breakdown

MERCHANT = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
BRANCH = "33333333-3333-3333-3333-333333333333"
JAN1 = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)
JAN2 = datetime(2026, 1, 2, 8, tzinfo=timezone.utc)
JAN3 = datetime(2026, 1, 3, 8, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def finance_postgres(tmp_path_factory):
    pgserver = pytest.importorskip("pgserver")
    server = pgserver.get_server(tmp_path_factory.mktemp("finance-postgres"), cleanup_mode="delete")
    yield server.get_uri()
    server.cleanup()


@pytest.fixture
async def report_db(finance_postgres, monkeypatch):
    schema = "finance_" + uuid4().hex
    bootstrap = await asyncpg.connect(finance_postgres)
    await bootstrap.execute(f"CREATE SCHEMA {schema}")
    pool = await asyncpg.create_pool(finance_postgres, min_size=1, max_size=4, server_settings={"search_path": schema})

    @asynccontextmanager
    async def connection():
        async with pool.acquire() as cx:
            yield cx

    @asynccontextmanager
    async def transaction():
        async with connection() as cx:
            async with cx.transaction():
                yield cx

    for mod in (finance, statements, wallets, compatibility):
        monkeypatch.setattr(mod, "get_connection", connection)
    monkeypatch.setattr(statements, "get_transaction", transaction)
    monkeypatch.setattr(
        finance.MerchantFinanceService,
        "_linked_account",
        AsyncMock(
            return_value={"status": "activated", "route_product_status": "activated", "linked_account_id": "acc_test"}
        ),
    )
    async with connection() as cx:
        await cx.execute("""
            CREATE TABLE rzp_payments (
              razorpay_payment_id text PRIMARY KEY,razorpay_order_id text,internal_order_id uuid,
              merchant_id uuid,branch_id uuid,amount_paise bigint,fee_paise bigint,tax_paise bigint,
              currency text DEFAULT 'INR',method text DEFAULT 'upi',upi_vpa text,bank_reference text,
              notes jsonb DEFAULT '{}',status text DEFAULT 'captured',captured bool DEFAULT true,
              captured_at timestamptz,created_at timestamptz);
            CREATE TABLE rzp_route_allocations (
              razorpay_payment_id text PRIMARY KEY,merchant_id uuid,merchant_paise bigint,created_at timestamptz DEFAULT now());
            CREATE TABLE rzp_route_transfers (
              transfer_id text PRIMARY KEY,razorpay_payment_id text,merchant_id uuid,amount_paise bigint,
              currency text DEFAULT 'INR',recipient_settlement_id text,status text DEFAULT 'processed',processed_at timestamptz,created_at timestamptz);
            CREATE TABLE rzp_refunds (
              refund_id text PRIMARY KEY,razorpay_payment_id text,merchant_id uuid,amount_paise bigint,
              status text,processed_at timestamptz,created_at timestamptz);
            CREATE TABLE rzp_settlements (
              settlement_id text PRIMARY KEY,merchant_id uuid,amount_paise bigint,utr text,status text,
              settled_at timestamptz,created_at timestamptz);
            CREATE TABLE rzp_settlement_payments (
              id bigserial PRIMARY KEY,settlement_id text,merchant_id uuid,razorpay_payment_id text,
              type text DEFAULT 'payment',amount_paise bigint,credit_paise bigint,debit_paise bigint DEFAULT 0,
              fee_paise bigint DEFAULT 0,tax_paise bigint DEFAULT 0,created_at timestamptz);
            CREATE TABLE payments (
              id uuid PRIMARY KEY,restaurant_id uuid,branch_id uuid,order_id uuid,method text,
              status text,amount numeric,currency text DEFAULT 'INR',paid_at timestamptz,created_at timestamptz,
              razorpay_payment_id text,razorpay_order_id text);
            CREATE TABLE refunds (
              id bigserial PRIMARY KEY,refund_uuid uuid DEFAULT gen_random_uuid(),merchant_id uuid,payment_id uuid,
              order_id uuid,amount numeric,currency text DEFAULT 'INR',kind text DEFAULT 'partial',status text,
              processed_at timestamptz,created_at timestamptz DEFAULT now());
            CREATE TABLE orders (id uuid PRIMARY KEY,restaurant_id uuid,branch_id uuid,status text,total_amount numeric,created_at timestamptz);
            CREATE TABLE restaurants(id uuid PRIMARY KEY,name text);
            CREATE TABLE merchant_statements (
              id uuid PRIMARY KEY DEFAULT gen_random_uuid(),merchant_id uuid,period_start timestamptz,period_end timestamptz,
              currency text,opening_balance numeric,total_credits numeric,total_debits numeric,closing_balance numeric,
              txn_count int,breakdown jsonb,status text,file_format text,file_path text,notes text,metadata jsonb,
              generated_at timestamptz DEFAULT now(),generated_by uuid,cancelled_at timestamptz,cancelled_by uuid,cancellation_reason text);
        """)
        await cx.execute("INSERT INTO restaurants VALUES($1::uuid,'Test Restaurant')", MERCHANT)
        # Historical allocation must stay 97.93%, even though today's new captures use 95%.
        await cx.execute(
            """INSERT INTO rzp_payments(razorpay_payment_id,merchant_id,branch_id,amount_paise,fee_paise,tax_paise,captured_at,created_at)
            VALUES('pay_old',$1::uuid,$2::uuid,10000,165,25,$3,$3),('pay_new',$1::uuid,NULL,10000,236,36,$4,$4),
                  ('pay_other',$5::uuid,NULL,90000,0,0,$3,$3)""",
            MERCHANT,
            BRANCH,
            JAN1,
            JAN2,
            OTHER,
        )
        await cx.execute(
            "INSERT INTO rzp_route_allocations(razorpay_payment_id,merchant_id,merchant_paise) VALUES('pay_old',$1::uuid,9793),('pay_new',$1::uuid,9500)",
            MERCHANT,
        )
        await cx.execute(
            "INSERT INTO rzp_route_transfers(transfer_id,razorpay_payment_id,merchant_id,amount_paise,created_at) VALUES('trf_old','pay_old',$1::uuid,9793,$2)",
            MERCHANT,
            JAN1,
        )
        await cx.execute(
            "INSERT INTO payments VALUES($1::uuid,$2::uuid,$3::uuid,NULL,'cash','completed',50,'INR',$4,$4)",
            uuid4(),
            MERCHANT,
            BRANCH,
            JAN1,
        )
    yield connection
    await pool.close()
    await bootstrap.execute(f"DROP SCHEMA {schema} CASCADE")
    await bootstrap.close()


def test_quotes_use_one_policy_cash_and_platform_loss():
    assert quote_fee(100)["net_settlement"] == 95
    assert quote_fee(100)["razorpay_cut_rate"] == "1.6500%"
    assert quote_fee(100, "cash")["total_deduction"] == 0
    assert quote_fee(200, "upi", "pos")["total_deduction"] == 0  # direct POS collection
    assert provisional_merchant_transfer_paise(10000, " CASH ") == (10000, 0, 0)
    # D1: taxable fee is the full 5% consideration; a gateway cost above the
    # deduction is a platform loss and never shrinks the fee or its GST.
    split = reporting_breakdown(100, actual_gateway_charges=8)
    assert split["platform_loss"] == 3
    assert (split["bittu_fee"], split["gst_on_fee"]) == (Decimal("4.24"), Decimal("0.76"))
    assert split["bittu_fee"] + split["gst_on_fee"] == split["total_deduction"] == 5
    assert split["net_to_merchant"] == 95


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True, "bad"])
def test_invalid_fee_inputs_rejected(bad):
    with pytest.raises(ValueError):
        quote_fee(bad)


async def test_wallet_reports_frozen_entitlement_and_cash_separately(report_db):
    wallet = await finance.merchant_finance_service.wallet_snapshot(MERCHANT)
    assert wallet["gross_sales"] == 250
    assert wallet["merchant_earned"] == 192.93
    assert wallet["total_deduction"] == 7.07
    assert wallet["pending_settlement"] == 192.93
    assert wallet["available_balance"] == 97.93
    assert wallet["cash_balance"] == 50
    assert wallet["online_settled_amount"] == 0
    check = await finance.merchant_finance_service.ledger_consistency_check(MERCHANT)
    assert check["consistent"] is True
    assert check["derived_balance"] == 192.93


async def test_actual_fee_already_includes_gateway_tax(report_db):
    fees = await finance.merchant_finance_service.fee_summary(MERCHANT)
    assert fees["total_deduction"] == 7.07
    assert fees["razorpay_cut"] == 4.01  # not 4.62: tax is a component of fee, not another deduction
    # D1: taxable fee + GST == the whole deduction; gateway cost only moves margin/loss.
    assert Decimal(str(fees["bittu_fee"])) + Decimal(str(fees["gst_on_fee"])) == Decimal("7.07")
    assert Decimal(str(fees["platform_loss"])) == Decimal("0.00")
    assert fees["fee_breakdown_complete"] is True


async def test_cash_and_online_share_one_pagination_and_branch_scope(report_db):
    svc = finance.merchant_finance_service
    ids = []
    for offset in range(3):
        page = await svc.list_transactions(MERCHANT, limit=1, offset=offset)
        assert page["total"] == 3 and len(page["items"]) == 1
        ids.append(page["items"][0]["payment_id"])
    assert len(set(ids)) == 3 and "pay_other" not in ids
    page = await svc.list_transactions(MERCHANT, branch_id=BRANCH)
    assert page["total"] == 2
    pending = await compatibility.statement_service.get_pending(MERCHANT)
    assert {row["payment_id"] for row in pending["items"]} == {"pay_old", "pay_new"}
    page = await svc.list_transactions(MERCHANT, payment_method="online")
    assert page["total"] == 2


async def test_zero_and_failed_bank_settlements_never_fall_back_to_gross(report_db):
    async with report_db() as cx:
        await cx.execute(
            "INSERT INTO rzp_settlements VALUES('setl_zero',$1::uuid,0,'utr_zero','processed',$2,$2),('setl_failed',$1::uuid,9500,NULL,'failed',NULL,$2)",
            MERCHANT,
            JAN2,
        )
        await cx.execute(
            "INSERT INTO rzp_settlement_payments(settlement_id,merchant_id,razorpay_payment_id,amount_paise,credit_paise,created_at) VALUES('setl_zero',$1::uuid,'pay_old',10000,0,$2)",
            MERCHANT,
            JAN2,
        )
    page = await finance.merchant_finance_service.list_settlements(MERCHANT)
    assert page["total"] == 2
    zero = next(row for row in page["items"] if row["settlement_id"] == "setl_zero")
    assert zero["merchant_amount"] == 0
    assert (await finance.merchant_finance_service.get_settlement(MERCHANT, "setl_zero"))["merchant_amount"] == 0
    wallet = await finance.merchant_finance_service.wallet_snapshot(MERCHANT)
    assert wallet["online_settled_amount"] == 0
    timeline = await finance.merchant_finance_service.settlement_timeline(MERCHANT, "setl_failed")
    assert len(timeline["events"]) == 1 and timeline["events"][0]["at"] is None
    assert timeline["history_complete"] is False


async def test_refunds_round_original_share_and_obey_cutoff(report_db):
    async with report_db() as cx:
        await cx.execute(
            "INSERT INTO rzp_refunds VALUES('ref_a','pay_old',$1::uuid,3333,'processed',$2,$2),('ref_b','pay_old',$1::uuid,6667,'processed',$3,$3)",
            MERCHANT,
            JAN2,
            JAN3,
        )
        await cx.execute("UPDATE rzp_payments SET status='refunded' WHERE razorpay_payment_id='pay_old'")
    svc = finance.merchant_finance_service
    before = await svc.wallet_snapshot(MERCHANT, to_date=date(2026, 1, 1))
    assert before["merchant_refund_share"] == 0
    after_first = await svc.wallet_snapshot(MERCHANT, to_date=date(2026, 1, 2))
    assert after_first["merchant_refund_share"] == 32.64
    wallet = await svc.wallet_snapshot(MERCHANT)
    assert wallet["merchant_refund_share"] == 97.93 and wallet["pending_settlement"] == 95
    entries = await svc._build_entries(MERCHANT, from_date=None, to_date=None)
    assert sum(Decimal(str(e["amount"])) for e in entries if e["source"] == "refund") == Decimal("97.93")
    assert (await svc.ledger_consistency_check(MERCHANT))["consistent"] is True


async def test_date_filtered_ledger_keeps_opening_balance(report_db):
    entries = await finance.merchant_finance_service._build_entries(
        MERCHANT, from_date=date(2026, 1, 2), to_date=date(2026, 1, 2)
    )
    assert entries[-1]["balance_after"] == 192.93  # includes Jan 1 entitlement


async def test_unknown_allocations_are_blank_in_export_and_block_final_statements(report_db):
    async with report_db() as cx:
        await cx.execute("DELETE FROM rzp_route_allocations WHERE razorpay_payment_id='pay_new'")
    svc = finance.merchant_finance_service
    page = await svc.list_transactions(MERCHANT)
    new = next(row for row in page["items"] if row["payment_id"] == "pay_new")
    assert new["merchant_amount"] is None and new["total_deduction"] is None
    assert (await svc.fee_summary(MERCHANT))["fee_breakdown_complete"] is False
    assert (await svc.ledger_consistency_check(MERCHANT))["consistent"] is False
    data, _ = await svc.export_csv(MERCHANT, from_date=date(2026, 1, 1), to_date=date(2026, 1, 3))
    row = next(row for row in csv.DictReader(io.StringIO(data)) if row["Payment ID"] == "pay_new")
    assert row["Merchant Amount"] == "" and row["Total Deduction"] == ""
    with pytest.raises(HTTPException) as exc:
        await statements.merchant_statement_service.generate(merchant_id=MERCHANT, period_start=JAN1, period_end=JAN3)
    assert exc.value.status_code == 422


async def test_statements_freeze_entries_and_preserve_zero_closing_balance(report_db):
    async with report_db() as cx:
        await cx.execute(
            "INSERT INTO rzp_settlements VALUES('setl_done',$1::uuid,19293,'utr_done','processed',$2,$2)",
            MERCHANT,
            JAN2,
        )
    svc = statements.merchant_statement_service
    st = await svc.generate(merchant_id=MERCHANT, period_start=JAN2, period_end=JAN3)
    assert st["opening_balance"] == 97.93 and st["closing_balance"] == 0
    assert st["entries_frozen"] is True
    first = await svc.to_csv(statement_id=st["id"], merchant_id=MERCHANT)
    async with report_db() as cx:
        await cx.execute("UPDATE rzp_settlements SET amount_paise=1 WHERE settlement_id='setl_done'")
    second = await svc.to_csv(statement_id=st["id"], merchant_id=MERCHANT)
    assert first["file_content"] == second["file_content"]
    entries = await svc.list_entries(statement_id=st["id"])
    assert entries[-1]["balance_after"] == 0 and len(entries) == st["txn_count"]
    with pytest.raises(HTTPException) as exc:
        await svc.list_entries(statement_id=st["id"], merchant_id=OTHER)
    assert exc.value.status_code == 404


async def test_legacy_reports_delegate_and_settlement_writes_are_retired(report_db):
    user = SimpleNamespace(restaurant_id=MERCHANT, branch_id=None, is_branch_user=False)
    wallet = await wallets.merchant_wallet_service.wallet(user)
    assert wallet["online_wallet"]["pending_settlement_net"] == 192.93
    assert wallet["platform_revenue"]["total_deduction_pct"] == "5.00%"
    summary = await compatibility.statement_service.get_summary(
        MERCHANT, from_date=date(2026, 1, 1), to_date=date(2026, 1, 3)
    )
    assert summary["total_deductions"] == 7.07 and summary["net_amount_credited"] == 0
    for method in (
        compatibility.statement_service.enqueue_payment_for_settlement,
        compatibility.statement_service.transition_settlement,
    ):
        with pytest.raises(HTTPException) as exc:
            await method()
        assert exc.value.status_code == 410


async def test_cash_never_creates_escrow_hold(monkeypatch):
    hold = AsyncMock()
    monkeypatch.setattr(escrow_integration.escrow_service, "hold_for_payment", hold)
    await escrow_integration.hold_payment_in_escrow(
        merchant_id=MERCHANT, payment_id=str(uuid4()), amount=100, method="CASH", strict=True
    )
    hold.assert_not_awaited()


async def test_capture_bridge_posts_receipt_and_fee_legs_without_escrow(monkeypatch):
    receipt = AsyncMock()
    posted: list[dict] = []

    async def fake_post(**kwargs):
        posted.append(kwargs)
        return {}

    monkeypatch.setattr(ledgers.merchant_ledger_integration, "post_payment_received", receipt)
    monkeypatch.setattr(ledgers.merchant_ledger_service, "post_entry", fake_post)
    conn = object()
    pid = str(uuid4())
    result = await ledgers.apply_payment_capture(
        conn=conn, merchant_id=MERCHANT, payment_id=pid, amount=100, method="upi",
        merchant_paise=9500, razorpay_payment_id="pay_x", payment_kind="razorpay",
    )
    assert receipt.call_args.kwargs["conn"] is conn and receipt.call_args.kwargs["strict"] is True
    # (a) ₹100 online -> fee ₹4.24 + GST ₹0.76, merchant ₹95.00; both legs in the same connection.
    assert result["fee_paise"] == 424 and result["gst_paise"] == 76
    legs = {p["transaction_type"]: p for p in posted}
    assert legs["fee_deduction"]["debit_amount"] == Decimal("4.24")
    assert legs["gst_deduction"]["debit_amount"] == Decimal("0.76")
    assert legs["fee_deduction"]["idempotency_key"] == f"fee_deduction:{pid}"
    assert all(p["conn"] is conn for p in posted)
    assert not any("escrow" in p["transaction_type"] for p in posted)

    # Unresolved allocation: receipt only, legs deferred to the backfill.
    posted.clear()
    deferred = await ledgers.apply_payment_capture(
        conn=conn, merchant_id=MERCHANT, payment_id=str(uuid4()), amount=100, method="upi", payment_kind="razorpay",
    )
    assert deferred["fee_paise"] is None and posted == []

    # Direct POS collection: receipt only, no deduction legs.
    direct = await ledgers.apply_payment_capture(
        conn=conn, merchant_id=MERCHANT, payment_id=str(uuid4()), amount=200, method="upi", payment_kind="direct",
    )
    assert direct == {"payment_kind": "direct", "fee_paise": 0, "gst_paise": 0} and posted == []

    receipt.side_effect = RuntimeError("ledger down")
    with pytest.raises(RuntimeError):
        await ledgers.apply_payment_capture(
            conn=conn, merchant_id=MERCHANT, payment_id=str(uuid4()), amount=100, method="upi", merchant_paise=9500,
        )


def test_recovery_fee_reversal_math_for_partial_refund():
    # (b) ₹100 payment, ₹40 refunded: merchant share 38.00, fee back 1.69 + GST 0.31 (= 2.00).
    from app.services.razorpay.fee_policy import gst_split_paise, proportional_share_paise

    merchant = proportional_share_paise(cumulative_paise=4000, previous_cumulative_paise=0, gross_paise=10000, share_paise=9500)
    assert merchant == 3800
    fee_total, gst_total = gst_split_paise(500)
    fee = proportional_share_paise(cumulative_paise=4000, previous_cumulative_paise=0, gross_paise=10000, share_paise=fee_total)
    gst = (4000 - merchant) - fee
    assert (fee, gst) == (169, 31) and fee + gst == 200
    # Second refund of ₹60 recovers rounding: cumulative legs sum to the full original split.
    merchant2 = proportional_share_paise(cumulative_paise=10000, previous_cumulative_paise=4000, gross_paise=10000, share_paise=9500)
    fee2 = proportional_share_paise(cumulative_paise=10000, previous_cumulative_paise=4000, gross_paise=10000, share_paise=fee_total)
    assert merchant + merchant2 == 9500 and fee + fee2 == fee_total == 424
    assert gst + ((6000 - merchant2) - fee2) == gst_total == 76


async def test_pending_transfer_is_not_an_available_balance(report_db):
    async with report_db() as cx:
        await cx.execute("UPDATE rzp_route_transfers SET status='pending' WHERE transfer_id='trf_old'")
    wallet = await finance.merchant_finance_service.wallet_snapshot(MERCHANT)
    assert wallet["pending_settlement"] == 192.93
    assert wallet["available_balance"] == 0


async def test_direct_pos_payments_report_without_deduction_and_partial_refunds(report_db):
    # (c) POS upi ₹200 on the merchant's own terminal: no razorpay ids, no deduction.
    direct_id = uuid4()
    async with report_db() as cx:
        await cx.execute(
            "INSERT INTO payments VALUES($1::uuid,$2::uuid,NULL,NULL,'upi','completed',200,'INR',$3,$3,NULL,NULL)",
            direct_id, MERCHANT, JAN2,
        )
        # A gateway payment row with razorpay ids must NOT be double counted as direct.
        await cx.execute(
            "INSERT INTO payments VALUES($1::uuid,$2::uuid,NULL,NULL,'upi','completed',100,'INR',$3,$3,'pay_new','order_new')",
            uuid4(), MERCHANT, JAN2,
        )
        await cx.execute(
            "INSERT INTO refunds(merchant_id,payment_id,amount,status,processed_at) VALUES($1::uuid,$2::uuid,50,'succeeded',$3)",
            MERCHANT, direct_id, JAN3,
        )
    svc = finance.merchant_finance_service
    wallet = await svc.wallet_snapshot(MERCHANT)
    assert wallet["direct_sales"] == 200 and wallet["cash_sales"] == 50 and wallet["online_sales"] == 200
    assert wallet["gross_sales"] == 450
    assert wallet["total_deduction"] == 7.07  # online only
    assert wallet["direct_refunds"] == 50 and wallet["direct_balance"] == 150
    assert wallet["settled_amount"] == 50 + 150  # cash + direct net; no online settlement yet
    page = await svc.list_transactions(MERCHANT, payment_method="direct")
    assert page["total"] == 1
    row = page["items"][0]
    assert row["payment_kind"] == "direct" and row["payment_method"] == "upi"
    assert row["total_deduction"] == 0 and row["bittu_fee"] == 0 and row["gst_on_fee"] == 0
    assert row["refund_amount"] == 50 and row["settlement_status"] == "settled"
    assert (await svc.fee_summary(MERCHANT))["total_deduction"] == 7.07


async def test_transfer_settlement_link_marks_payment_settled_without_recon(report_db):
    async with report_db() as cx:
        await cx.execute(
            "INSERT INTO rzp_settlements VALUES('setl_linked',$1::uuid,9793,'utr_linked','processed',$2,$2)",
            MERCHANT,
            JAN2,
        )
        await cx.execute(
            "UPDATE rzp_route_transfers SET recipient_settlement_id='setl_linked' WHERE transfer_id='trf_old'"
        )
    page = await finance.merchant_finance_service.list_transactions(MERCHANT)
    old = next(row for row in page["items"] if row["payment_id"] == "pay_old")
    assert old["settlement_status"] == "settled" and old["settlement_id"] == "setl_linked"
    assert (await finance.merchant_finance_service.wallet_snapshot(MERCHANT))["online_settled_amount"] == 97.93
    row = await finance.merchant_finance_service.get_settlement(MERCHANT, "setl_linked")
    assert row["reconciliation_complete"] is False


def test_fee_estimate_loads_dotenv_and_environment_override(tmp_path, monkeypatch):
    import runpy
    from pathlib import Path

    source = Path("app/services/razorpay/fee_policy.py").resolve()
    (tmp_path / ".env").write_text("BITTU_RZP_ESTIMATE_RATE=0.02\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BITTU_RZP_ESTIMATE_RATE", raising=False)
    policy = runpy.run_path(str(source))
    assert policy["quote_fee"](100)["razorpay_cut"] == 2
    assert policy["quote_fee"](100)["net_settlement"] == 95
    monkeypatch.setenv("BITTU_RZP_ESTIMATE_RATE", "0.03")
    policy = runpy.run_path(str(source))
    assert policy["quote_fee"](100)["razorpay_cut"] == 3
    assert policy["rate_card"]()["razorpay_cut_rate_pct"] == "3.0000%"


async def test_platform_settlement_does_not_confirm_merchant_bank_payout(report_db):
    async with report_db() as cx:
        await cx.execute(
            "INSERT INTO rzp_settlements VALUES('setl_platform',$1::uuid,9793,'utr_platform','processed',$2,$2)",
            OTHER,
            JAN2,
        )
        await cx.execute(
            "INSERT INTO rzp_settlement_payments(settlement_id,merchant_id,razorpay_payment_id,amount_paise,credit_paise,created_at) VALUES('setl_platform',$1::uuid,'pay_old',10000,9793,$2)",
            MERCHANT,
            JAN2,
        )
    svc = finance.merchant_finance_service
    assert (await svc.wallet_snapshot(MERCHANT))["online_settled_amount"] == 0
    assert (await svc.get_settlement(MERCHANT, "setl_platform"))["status"] == "pending"
    page = await svc.list_transactions(MERCHANT)
    old = next(row for row in page["items"] if row["payment_id"] == "pay_old")
    assert old["settlement_status"] == "processing"


@pytest.fixture
async def saas_invoice_db(report_db, monkeypatch):
    from app.services import invoice_pdf_service as pdf

    monkeypatch.setattr(pdf, "get_connection", report_db)
    monkeypatch.setattr(pdf, "get_service_connection", report_db)
    async with report_db() as cx:
        await cx.execute("""
            ALTER TABLE restaurants ADD COLUMN gst_number text, ADD COLUMN address text, ADD COLUMN city text,
                                    ADD COLUMN state text, ADD COLUMN pincode text;
            CREATE TABLE bittu_saas_invoices (
              id bigserial PRIMARY KEY, invoice_number text UNIQUE, merchant_id uuid, period_year int, period_month int,
              currency text, gross_collected numeric, online_gross numeric DEFAULT 0, total_deduction numeric DEFAULT 0,
              bittu_fee_amount numeric, gst_on_fee_amount numeric, total_payable numeric,
              refund_credit_fee numeric DEFAULT 0, refund_credit_gst numeric DEFAULT 0, refund_count int DEFAULT 0,
              net_payable numeric DEFAULT 0, txn_count int, unresolved_count int DEFAULT 0, status text,
              basis text, source text, frozen_at timestamptz, issued_at timestamptz DEFAULT now(),
              updated_at timestamptz DEFAULT now(), UNIQUE (merchant_id, period_year, period_month));
            CREATE TABLE bittu_saas_credit_notes (
              id bigserial PRIMARY KEY, credit_note_number text UNIQUE, invoice_id bigint, merchant_id uuid,
              refund_id text UNIQUE, razorpay_payment_id text, currency text, recovered_amount numeric,
              fee_credit numeric, gst_credit numeric, total_credit numeric, reason text, metadata jsonb,
              created_at timestamptz DEFAULT now());
        """)
    return report_db


async def test_saas_invoice_full_consideration_and_refund_credit(saas_invoice_db):
    from app.services import invoice_pdf_service as pdf

    # Only online rows count: pay_old (₹100 → 97.93) + pay_new (₹100 → 95); the ₹50 cash row is excluded.
    agg = await pdf._aggregate_saas_period(merchant_id=MERCHANT, year=2026, month=1)
    assert agg["online_gross"] == 200 and agg["txn_count"] == 2 and agg["unresolved_count"] == 0
    # pay_new: deduction 500p → fee 424 + gst 76 (a). pay_old: deduction 207p → fee 175 + gst 32.
    assert agg["total_deduction"] == Decimal("7.07")
    assert (agg["bittu_fee"], agg["gst_on_fee"]) == (Decimal("5.99"), Decimal("1.08"))
    assert agg["bittu_fee"] + agg["gst_on_fee"] == agg["total_deduction"]
    inv = await pdf._persist_saas_invoice(merchant_id=MERCHANT, year=2026, month=1, currency="INR", agg=agg)
    assert inv["status"] == "issued" and inv["frozen_at"] is not None and inv["basis"] == "full_consideration"
    assert Decimal(str(inv["net_payable"])) == Decimal("7.07")

    # ₹40 refund on pay_new processed in Jan → credit of fee 1.69 + GST 0.31 (b) — but the month is
    # frozen, so it must become a credit note instead of rewriting the issued invoice.
    async with saas_invoice_db() as cx:
        await cx.execute(
            "INSERT INTO rzp_refunds VALUES('ref_late','pay_new',$1::uuid,4000,'processed',$2,$2)", MERCHANT, JAN3
        )
    agg2 = await pdf._aggregate_saas_period(merchant_id=MERCHANT, year=2026, month=1)
    assert (agg2["refund_credit_fee"], agg2["refund_credit_gst"]) == (Decimal("1.69"), Decimal("0.31"))
    inv2 = await pdf._persist_saas_invoice(merchant_id=MERCHANT, year=2026, month=1, currency="INR", agg=agg2)
    assert Decimal(str(inv2["net_payable"])) == Decimal("7.07")  # issued amounts untouched
    assert inv2["status"] == "credit_noted" and len(inv2["credit_notes"]) == 1
    note = inv2["credit_notes"][0]
    assert (Decimal(str(note["fee_credit"])), Decimal(str(note["gst_credit"]))) == (Decimal("1.69"), Decimal("0.31"))
    inv3 = await pdf._persist_saas_invoice(merchant_id=MERCHANT, year=2026, month=1, currency="INR", agg=agg2)
    assert len(inv3["credit_notes"]) == 1  # idempotent


async def test_saas_invoice_with_unresolved_allocation_stays_draft(saas_invoice_db):
    from app.services import invoice_pdf_service as pdf

    async with saas_invoice_db() as cx:
        await cx.execute("DELETE FROM rzp_route_allocations WHERE razorpay_payment_id='pay_new'")
    agg = await pdf._aggregate_saas_period(merchant_id=MERCHANT, year=2026, month=1)
    assert agg["unresolved_count"] == 1 and agg["txn_count"] == 2
    inv = await pdf._persist_saas_invoice(merchant_id=MERCHANT, year=2026, month=1, currency="INR", agg=agg)
    assert inv["status"] == "draft" and inv["frozen_at"] is None and inv["unresolved_count"] == 1  # (f)
    pdf_bytes, filename = await pdf.render_saas_invoice(merchant_id=MERCHANT, year=2026, month=1)
    assert filename.endswith("_draft.pdf") and pdf_bytes[:4] == b"%PDF"


async def test_settlement_processed_posts_payout_legs_once_per_payment(report_db, monkeypatch):
    posted: list[dict] = []

    async def fake_post(**kwargs):
        posted.append(kwargs)
        return {}

    monkeypatch.setattr(ledgers, "get_service_connection", report_db)
    monkeypatch.setattr(ledgers.merchant_ledger_service, "post_entry", fake_post)
    canonical = uuid4()
    async with report_db() as cx:
        await cx.execute(
            "INSERT INTO payments VALUES($1::uuid,$2::uuid,NULL,NULL,'upi','completed',100,'INR',$3,$3,'pay_new','order_new')",
            canonical, MERCHANT, JAN2,
        )
        await cx.execute(
            "INSERT INTO rzp_settlements VALUES('setl_a',$1::uuid,9500,'utr_a','processed',$2,$2)", MERCHANT, JAN3
        )
        # Recon line: merchant credited 95.00 for pay_new; transfer link for pay_old (no canonical row → skipped)
        await cx.execute(
            "INSERT INTO rzp_settlement_payments(settlement_id,merchant_id,razorpay_payment_id,amount_paise,credit_paise,created_at) "
            "VALUES('setl_a',$1::uuid,'pay_new',10000,9500,$2)", MERCHANT, JAN3,
        )
        await cx.execute("UPDATE rzp_route_transfers SET recipient_settlement_id='setl_a' WHERE transfer_id='trf_old'")
    result = await ledgers.apply_settlement_processed(settlement_id="setl_a", merchant_id=MERCHANT)
    assert result["posted"] == 1 and result["skipped_without_canonical_payment"] == 1
    leg = posted[0]
    assert leg["transaction_type"] == "settlement_completed" and leg["debit_amount"] == Decimal("95.00")
    assert leg["idempotency_key"] == f"settlement_completed:setl_a:{canonical}" and leg["utr_number"] == "utr_a"
    # A later refund recovered through the next settlement nets negative → credit settlement_reversed.
    posted.clear()
    async with report_db() as cx:
        await cx.execute(
            "INSERT INTO rzp_settlements VALUES('setl_b',$1::uuid,0,'utr_b','processed',$2,$2)", MERCHANT, JAN3
        )
        await cx.execute(
            "INSERT INTO rzp_settlement_payments(settlement_id,merchant_id,razorpay_payment_id,type,amount_paise,credit_paise,debit_paise,created_at) "
            "VALUES('setl_b',$1::uuid,'pay_new','refund',4000,0,3800,$2)", MERCHANT, JAN3,
        )
    result = await ledgers.apply_settlement_processed(settlement_id="setl_b", merchant_id=MERCHANT)
    assert result["posted"] == 1
    assert posted[0]["transaction_type"] == "settlement_reversed" and posted[0]["credit_amount"] == Decimal("38.00")
