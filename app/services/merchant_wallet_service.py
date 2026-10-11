"""Compatibility reports backed by the shared Route finance projection."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.database import get_connection
from app.core.exceptions import ValidationError
from app.services.razorpay.fee_policy import is_cash_method, quote_fee, rate_card
from app.services.razorpay.merchant_finance_service import merchant_finance_service as finance


def _restaurant_id(user):
    if not user.restaurant_id:
        raise ValidationError("Active restaurant context is required.")
    return str(user.restaurant_id)


def _branch(user):
    return user.branch_id if user.is_branch_user else None


class MerchantWalletService:
    async def wallet(self, user, *, as_of_date=None, from_date=None, to_date=None):
        rid = _restaurant_id(user)
        branch = _branch(user)
        end = to_date if from_date is not None or to_date is not None else as_of_date
        activity = await finance.wallet_snapshot(rid, from_date=from_date, to_date=end, branch_id=branch)
        lifetime = await finance.wallet_snapshot(rid, to_date=end, branch_id=branch) if from_date else activity
        fees = await finance.fee_summary(rid, to_date=end, branch_id=branch)
        async with get_connection() as conn:
            orders = await conn.fetchrow(
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(total_amount),0) AS sales FROM orders
                WHERE restaurant_id=$1::uuid AND LOWER(status::text) IN ('confirmed','preparing','ready','completed','served','delivered')
                  AND ($2::date IS NULL OR (created_at AT TIME ZONE 'Asia/Kolkata')::date >= $2)
                  AND ($3::date IS NULL OR (created_at AT TIME ZONE 'Asia/Kolkata')::date <= $3)
                  AND ($4::uuid IS NULL OR branch_id=$4::uuid)
            """,
                rid,
                from_date,
                end,
                branch,
            )
        rates = rate_card()
        return {
            "restaurant_id": rid,
            "as_of": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
            "as_of_date": as_of_date.isoformat() if as_of_date else None,
            "from_date": from_date.isoformat() if from_date else None,
            "to_date": end.isoformat() if end else None,
            "sales": {
                "total_orders": int(orders["count"]),
                "total_sales": float(orders["sales"]),
                "cash_sales": activity["cash_sales"],
                "online_sales": activity["online_sales"],
            },
            "cash_wallet": {
                "balance": lifetime["cash_balance"],
                "collected_lifetime": lifetime["cash_sales"],
                "refunded_lifetime": lifetime["cash_refunds"],
                "note": "Recorded cash receipts less recorded cash refunds; physical deposits are not tracked here.",
            },
            "online_wallet": {
                "pending_settlement_gross": None,
                "pending_settlement_net": lifetime["pending_settlement"],
                "settled_lifetime_net": lifetime["online_settled_lifetime"],
                "settled_lifetime_gross": None,
                "captured_lifetime": lifetime["online_sales"],
                "in_clearing": lifetime["pending_settlement"],
                "available_balance": lifetime["available_balance"],
                "balance_discrepancy": lifetime["balance_discrepancy"],
                "unresolved_payment_count": lifetime["unresolved_payment_count"],
            },
            "platform_revenue": {
                "fee_collected_lifetime": fees["bittu_fee"],
                "gst_on_fee_lifetime": fees["gst_on_fee"],
                "total_deduction_pct": rates["total_deduction_rate_pct"],
                "fee_pct": rates["bittu_fee_rate_pct"],
                "gst_pct_on_fee": rates["gst_rate_pct"],
                "fee_breakdown": fees,
                "basis": fees["basis"],
            },
            "source": "razorpay_route_mirror",
            "wallet_status": activity["wallet_status"],
            "balance_scope": "lifetime_through_to_date",
        }

    async def quote_fee(self, gross_amount, method="upi"):
        try:
            result = quote_fee(gross_amount, method)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return {
            **result,
            "platform_fee": result["bittu_fee"],
            "method": method,
            "channel": "cash" if is_cash_method(method) else "online",
        }

    async def list_settlements(self, user, **kwargs):
        return await finance.list_settlements(_restaurant_id(user), branch_id=_branch(user), **kwargs)

    async def get_settlement(self, user, settlement_id):
        return await finance.get_settlement(_restaurant_id(user), settlement_id, branch_id=_branch(user))

    async def list_transactions(self, user, **kwargs):
        return await finance.list_transactions(_restaurant_id(user), branch_id=_branch(user), **kwargs)

    async def daily_closing(self, user, closing_date=None):
        day = closing_date or datetime.now(ZoneInfo("Asia/Kolkata")).date()
        return await self.wallet(user, from_date=day, to_date=day)

    async def platform_revenue_report(self, user, from_date=None, to_date=None):
        fees = await finance.fee_summary(
            _restaurant_id(user), branch_id=_branch(user), from_date=from_date, to_date=to_date
        )
        return {
            **fees,
            "platform_fee_revenue": fees["bittu_fee"],
            "gst_on_fee_collected": fees["gst_on_fee"],
            "rate_card": rate_card(),
            "period": {"from": str(from_date) if from_date else None, "to": str(to_date) if to_date else None},
        }

    async def fee_gst_report(self, user, from_date=None, to_date=None):
        fees = await self.platform_revenue_report(user, from_date, to_date)
        return {
            **fees,
            "total_fee": fees["bittu_fee"],
            "total_gst_on_fee": fees["gst_on_fee"],
            "note": "Payment-level allocation breakdown before refund adjustments. Tax invoices and credit notes determine tax reporting.",
        }


merchant_wallet_service = MerchantWalletService()
