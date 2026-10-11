"""Compatibility API for Route-backed merchant statements.

All live reads use the same Razorpay projection as /merchant-wallet. Legacy
bittu_settlements remain historical records; this service cannot pay or post
settlement journals. Gateway webhooks and reconciliation own settlement state.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.core.database import get_connection
from app.core.exceptions import ValidationError
from app.services.razorpay.fee_policy import quote_fee, rate_card, reporting_breakdown
from app.services.razorpay.merchant_finance_service import merchant_finance_service as finance


def _calc_settlement_breakdown(gross, *, actual_gateway_charges=None):
    return reporting_breakdown(gross, actual_gateway_charges=actual_gateway_charges)


def _calc_fee(gross):
    br = _calc_settlement_breakdown(gross)
    return br["bittu_fee"], br["gst_on_fee"], br["net_to_merchant"]


def _window(first, last):
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    first, last = first or today.replace(day=1), last or today
    if first > last:
        raise ValidationError("from_date must be <= to_date")
    return first, last


def _transaction(row):
    return {
        **row,
        "id": row["payment_id"],
        "order_id": row["internal_order_id"] or row["order_id"],
        "fee_amount": row["bittu_fee"],
        "gst_amount": row["gst_on_fee"],
        "net_amount": row["merchant_amount"],
        "transaction_type": "payment",
        "order_reference": row["order_id"],
        "expected_settlement_at": None,
        "settlement_cycle": None,
        "settlement_reference": row["settlement_id"],
    }


def _settlement(row):
    return {
        **row,
        "id": row["settlement_id"],
        "settlement_reference": row["settlement_id"],
        "settlement_status": "settled" if row["status"] == "processed" else row["status"],
        "net_settlement_amount": row["merchant_amount"],
        "bittu_fee_amount": None,
        "gst_amount": None,
        "bank_reference_number": row["utr"],
        "expected_settlement_at": None,
        "settlement_cycle": None,
        "is_virtual": False,
    }


def _retired():
    raise HTTPException(
        status_code=410,
        detail=("Legacy Bittu settlement writes are retired. Razorpay Route owns transfers and bank settlement state."),
    )


class StatementService:
    async def get_summary(self, restaurant_id, branch_id=None, from_date=None, to_date=None):
        first, last = _window(from_date, to_date)
        wallet = await finance.wallet_snapshot(restaurant_id, branch_id=branch_id, from_date=first, to_date=last)
        fees = await finance.fee_summary(restaurant_id, branch_id=branch_id, from_date=first, to_date=last)
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        collection = await finance.wallet_snapshot(restaurant_id, branch_id=branch_id, from_date=today, to_date=today)
        return {
            "period": {"from": first.isoformat(), "to": last.isoformat()},
            "total_received": wallet["gross_sales"],
            "today_collection": collection["gross_sales"],
            "pending_settlement": wallet["pending_settlement"],
            "settled_amount": wallet["online_settled_amount"],
            "net_amount_credited": wallet["online_settled_amount"],
            "total_bittu_charges": fees["bittu_fee"],
            "gst_on_charges": fees["gst_on_fee"],
            "razorpay_charges": fees["razorpay_cut"],
            "total_deductions": fees["total_deduction"],
            "upcoming_settlement_eta": None,
            "upcoming_settlement_amount": wallet["pending_settlement"],
            "upcoming_settlement_message": "Settlement timing follows the linked account Razorpay schedule.",
            "fee_info": rate_card(),
            "fee_breakdown": fees,
            "wallet": wallet,
            "source": "razorpay_route_mirror",
        }

    async def get_transactions(
        self,
        restaurant_id,
        branch_id=None,
        from_date=None,
        to_date=None,
        settlement_status=None,
        payment_method=None,
        search=None,
        sort_by="created_at",
        sort_dir="desc",
        limit=50,
        offset=0,
    ):
        page = await finance.list_transactions(
            restaurant_id,
            branch_id=branch_id,
            from_date=from_date,
            to_date=to_date,
            settlement_status=settlement_status,
            payment_method=payment_method,
            search=search,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
        return {**page, "items": [_transaction(row) for row in page["items"]]}

    async def get_settlements(
        self,
        restaurant_id,
        branch_id=None,
        from_date=None,
        to_date=None,
        status=None,
        cycle=None,
        sort_by="created_at",
        sort_dir="desc",
        limit=50,
        offset=0,
    ):
        if cycle:
            raise ValidationError("Route settlement cycles are managed by Razorpay; T+0/T+1 batch filters are retired.")
        page = await finance.list_settlements(
            restaurant_id,
            branch_id=branch_id,
            from_date=from_date,
            to_date=to_date,
            status=status,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
        return {**page, "items": [_settlement(row) for row in page["items"]]}

    async def get_settlement_detail(self, settlement_id, restaurant_id, branch_id=None):
        row = await finance.get_settlement(restaurant_id, settlement_id, branch_id=branch_id)
        timeline = await finance.settlement_timeline(restaurant_id, settlement_id, branch_id=branch_id)
        return {
            **_settlement(row),
            "transactions": row["payments"],
            "timeline": timeline["events"],
            "history_complete": False,
        }

    async def get_pending(self, restaurant_id, branch_id=None):
        wallet = await finance.wallet_snapshot(restaurant_id, branch_id=branch_id)
        page = await self.get_transactions(restaurant_id, branch_id=branch_id, settlement_status="unsettled", limit=50)
        return {
            "pending_amount": wallet["pending_settlement"],
            "total_pending_amount": wallet["pending_settlement"],
            "items": page["items"],
            "total": page["total"],
            "has_more": page["has_more"],
            "available_balance": wallet["available_balance"],
            "source": wallet["source"],
            "expected_settlement_at": None,
            "unresolved_payment_count": wallet["unresolved_payment_count"],
        }

    async def get_settlement_timeline(self, settlement_id, restaurant_id, branch_id=None):
        return await finance.settlement_timeline(restaurant_id, settlement_id, branch_id=branch_id)

    async def export_statement(
        self, restaurant_id, branch_id=None, from_date=None, to_date=None, settlement_status=None
    ):
        first, last = _window(from_date, to_date)
        async with get_connection() as conn:
            restaurant_name = await conn.fetchval("SELECT name FROM restaurants WHERE id=$1::uuid", restaurant_id)
        transactions = []
        offset = 0
        while True:
            page = await self.get_transactions(
                restaurant_id,
                branch_id=branch_id,
                from_date=first,
                to_date=last,
                settlement_status=settlement_status,
                limit=10000,
                offset=offset,
                sort_dir="asc",
            )
            transactions.extend(page["items"])
            if not page["has_more"]:
                break
            offset += len(page["items"])
        settlements = []
        offset = 0
        while True:
            page = await self.get_settlements(
                restaurant_id,
                branch_id=branch_id,
                from_date=first,
                to_date=last,
                limit=10000,
                offset=offset,
                sort_dir="asc",
            )
            settlements.extend(page["items"])
            if not page["has_more"]:
                break
            offset += len(page["items"])
        summary = await self.get_summary(restaurant_id, branch_id=branch_id, from_date=first, to_date=last)
        return {
            "meta": {
                "restaurant_name": restaurant_name,
                "restaurant_id": restaurant_id,
                "period": summary["period"],
                "generated_at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
                "transaction_count": len(transactions),
                "settlement_count": len(settlements),
                "source": "razorpay_route_mirror",
            },
            "summary": summary,
            "transactions": transactions,
            "settlements": settlements,
            "columns": [
                "created_at",
                "payment_id",
                "order_id",
                "gross_amount",
                "total_deduction",
                "fee_amount",
                "gst_amount",
                "net_amount",
                "payment_method",
                "settlement_status",
            ],
        }

    async def enqueue_payment_for_settlement(self, **kwargs):
        _retired()

    async def transition_settlement(self, *args, **kwargs):
        _retired()

    def calculate_fee(self, gross_amount, method=None):
        try:
            return quote_fee(gross_amount, method)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc


statement_service = StatementService()
