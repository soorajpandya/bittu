"""Route pricing: merchant receives 95%; gateway charges reduce Bittu's share.

All amounts are integer paise. Estimates are reporting-only. A negative Bittu
balance represents a platform loss, never an additional merchant deduction.

GST basis (D1, "full_consideration"): Bittu's taxable supply to the merchant is
the entire 5% deduction. Razorpay's charges are Bittu's input cost and never
reduce the taxable fee shown on the merchant's tax invoice.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

GST_RATE = Decimal("0.18")
TOTAL_DEDUCTION_RATE = Decimal("0.05")
PLATFORM_FEE_BASE_RATE = TOTAL_DEDUCTION_RATE / (1 + GST_RATE)  # ex-GST taxable fee rate
GST_ON_FEE_RATE = TOTAL_DEDUCTION_RATE - PLATFORM_FEE_BASE_RATE
RAZORPAY_TOTAL_RATE = Decimal("0.0165")
# GST-inclusive residual Bittu keeps after the estimated gateway cost. Margin
# estimate only; it is NOT the taxable fee.
PLATFORM_RESIDUAL_RATE_ESTIMATE = TOTAL_DEDUCTION_RATE - RAZORPAY_TOTAL_RATE
FEE_BASIS = "full_consideration"


class _ReportingSettings(BaseSettings):
    # Load only reporting configuration, without requiring database or gateway
    # credentials just to quote a fee. Environment variables override .env.
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")
    BITTU_RZP_ESTIMATE_RATE: Decimal = Field(Decimal("0.0165"), ge=0, allow_inf_nan=False)


_DEFAULT_RZP_ESTIMATE_RATE = _ReportingSettings().BITTU_RZP_ESTIMATE_RATE
_CASH_METHODS = frozenset({"cash", "counter", "cod"})
# Methods that only ever reach us through the Razorpay gateway.
_GATEWAY_METHODS = frozenset({"online", "razorpay", "gateway", "netbanking"})
_GATEWAY_SOURCES = frozenset({"razorpay", "online", "gateway"})
# POS aliases -> payments.method enum values.
POS_METHOD_ALIASES = {
    "cash": "cash", "counter": "cash", "cod": "cash",
    "upi": "upi", "qr_pay": "upi", "qr": "upi", "qr_code": "upi",
    "card": "card", "swipe": "card", "credit": "card", "debit": "card",
    "credit_card": "card", "debit_card": "card",
    "wallet": "wallet",
    "online": "online", "razorpay": "online", "gateway": "online", "netbanking": "online",
}


def _q_paise(amount_paise: Decimal | int) -> int:
    return int(Decimal(amount_paise).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def normalize_payment_method(method: str | None) -> str:
    """Map any POS alias to the payments.method enum; unknown values are cash."""
    return POS_METHOD_ALIASES.get((method or "").strip().lower(), "cash")


def classify_method(method: str | None, source: str | None = None) -> str:
    """Return "cash" | "direct" | "razorpay" for a payment.

    * cash     — cash/counter/COD; no deduction, settled in the till.
    * razorpay — captured through the Razorpay gateway (Route split applies).
    * direct   — merchant's own UPI/card/wallet terminal recorded at the POS;
                 no gateway, no deduction, no escrow.

    Without a source the gateway context is assumed (quotes, webhooks,
    Razorpay mirrors). POS checkout passes source="pos" so that upi/card/
    wallet collected on the merchant's own terminal classify as direct.
    """
    src = (source or "").strip().lower()
    m = (method or "").strip().lower()
    if src == "cash" or m in _CASH_METHODS:
        return "cash"
    if src == "direct":
        return "direct"
    if not src or src in _GATEWAY_SOURCES:
        return "razorpay"
    return "razorpay" if m in _GATEWAY_METHODS else "direct"


def gst_split_paise(deduction_paise: int) -> tuple[int, int]:
    """Split a GST-inclusive deduction into (taxable_fee, gst); conserves paise."""
    if isinstance(deduction_paise, bool) or not isinstance(deduction_paise, int) or deduction_paise < 0:
        raise ValueError("deduction must be non-negative integer paise")
    fee = _q_paise(Decimal(deduction_paise) / (1 + GST_RATE))
    return fee, deduction_paise - fee


def proportional_share_paise(*, cumulative_paise: int, previous_cumulative_paise: int, gross_paise: int, share_paise: int) -> int:
    """Incremental share of `share_paise` for a refund that moves the cumulative
    refunded amount from previous to cumulative. FLOOR on the cumulative total
    recovers rounding on the final refund (same rule Route reversals use)."""
    for value in (cumulative_paise, previous_cumulative_paise, gross_paise, share_paise):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("proportional share inputs must be non-negative integer paise")
    if gross_paise == 0:
        return 0
    now = min(cumulative_paise, gross_paise) * share_paise // gross_paise
    before = min(previous_cumulative_paise, gross_paise) * share_paise // gross_paise
    return now - before


def merchant_transfer_paise(gross_paise: int) -> int:
    if isinstance(gross_paise, bool) or not isinstance(gross_paise, int) or gross_paise < 0:
        raise ValueError("gross must be non-negative integer paise")
    # Round the total deduction once; Bittu absorbs gateway price changes.
    return gross_paise - _q_paise(Decimal(gross_paise) * TOTAL_DEDUCTION_RATE)


def split_payment_paise(gross_paise: int, gateway_charges_paise: int) -> tuple[int, int, int]:
    merchant = merchant_transfer_paise(gross_paise)
    if (
        isinstance(gateway_charges_paise, bool)
        or not isinstance(gateway_charges_paise, int)
        or gateway_charges_paise < 0
    ):
        raise ValueError("gateway charges must be non-negative integer paise")
    return merchant, gross_paise - merchant - gateway_charges_paise, gateway_charges_paise


def estimate_rzp_charges_paise(gross_paise: int, method: str | None = None) -> int:
    merchant_transfer_paise(gross_paise)  # validate units
    if is_cash_method(method):
        return 0
    return _q_paise(Decimal(gross_paise) * _DEFAULT_RZP_ESTIMATE_RATE)


def bittu_fee_paise(gross_paise: int) -> int:
    """Estimated Bittu residual, not a fixed additional charge."""
    return provisional_merchant_transfer_paise(gross_paise)[1]


def provisional_merchant_transfer_paise(gross_paise: int, method: str | None = None) -> tuple[int, int, int]:
    if is_cash_method(method):
        merchant_transfer_paise(gross_paise)  # validate units
        return gross_paise, 0, 0
    return split_payment_paise(gross_paise, estimate_rzp_charges_paise(gross_paise, method))


def is_cash_method(method: str | None) -> bool:
    return (method or "").strip().lower() in _CASH_METHODS


def rupees_to_paise(amount) -> int:
    """Validate currency input and round once at the paise boundary."""
    if isinstance(amount, bool):
        raise ValueError("amount must be a finite non-negative currency amount")
    try:
        value = Decimal(str(amount))
    except InvalidOperation as exc:
        raise ValueError("amount must be a finite non-negative currency amount") from exc
    if not value.is_finite() or value < 0:
        raise ValueError("amount must be a finite non-negative currency amount")
    return _q_paise(value * 100)


def reporting_breakdown(
    gross,
    *,
    method: str | None = None,
    source: str | None = None,
    actual_gateway_charges=None,
    merchant_paise: int | None = None,
) -> dict:
    """Preview or explain a frozen allocation without repricing history.

    The taxable fee and its GST are carved out of the total deduction
    (full consideration). Gateway charges are GST-inclusive input costs that
    only move Bittu's margin: residual = deduction − gateway, and a negative
    residual is reported as platform_loss. Estimates are never audited revenue.
    """
    gross_paise = rupees_to_paise(gross)
    kind = classify_method(method, source)
    if kind != "razorpay":
        merchant_paise, gateway = gross_paise, 0
    else:
        if merchant_paise is None:
            merchant_paise = merchant_transfer_paise(gross_paise)
        if (
            isinstance(merchant_paise, bool)
            or not isinstance(merchant_paise, int)
            or not 0 <= merchant_paise <= gross_paise
        ):
            raise ValueError("merchant entitlement must be integer paise within gross")
        gateway = (
            rupees_to_paise(actual_gateway_charges)
            if actual_gateway_charges is not None
            else estimate_rzp_charges_paise(gross_paise, method)
        )
    deduction = gross_paise - merchant_paise
    fee, gst = gst_split_paise(deduction)
    residual = deduction - gateway

    def money(value):
        return Decimal(value) / 100

    return {
        "gross": money(gross_paise),
        "total_deduction": money(deduction),
        "net_to_merchant": money(merchant_paise),
        "bittu_fee": money(fee),
        "gst_on_fee": money(gst),
        "fee_basis": FEE_BASIS,
        "razorpay_cut": money(gateway),
        "platform_pool": money(residual),
        "platform_margin": money(max(residual, 0)),
        "platform_loss": money(max(-residual, 0)),
        "payment_kind": kind,
        "gateway_charges_estimated": kind == "razorpay" and actual_gateway_charges is None,
    }


def rate_card(method: str | None = None, source: str | None = None) -> dict:
    kind = classify_method(method, source)
    deducted = kind == "razorpay"
    gateway = _DEFAULT_RZP_ESTIMATE_RATE if deducted else Decimal(0)
    total = TOTAL_DEDUCTION_RATE if deducted else Decimal(0)
    base = total / (1 + GST_RATE)
    return {
        "total_deduction_rate_pct": f"{total * 100:.2f}%",
        "bittu_fee_rate_pct": f"{base * 100:.4f}%",
        "gst_on_fee_rate_pct": f"{(total - base) * 100:.4f}%",
        "gst_rate_pct": f"{GST_RATE * 100:.0f}%",
        "fee_basis": FEE_BASIS,
        "razorpay_cut_rate_pct": f"{gateway * 100:.4f}%",
        "platform_margin_rate_pct": f"{max(total - gateway, Decimal(0)) * 100:.4f}%",
        "gateway_rate_is_estimate": deducted,
        "payment_kind": kind,
        "description": "No deduction; collected directly by the merchant."
        if not deducted
        else (
            "5% total deduction = 4.2373% platform fee + 18% GST on that fee. "
            "Gateway charges are Bittu's cost and never change the merchant's 95%."
        ),
    }


def quote_fee(gross, method: str | None = None, source: str | None = None) -> dict:
    br = reporting_breakdown(gross, method=method, source=source)
    rates = rate_card(method, source)
    return {
        "gross_amount": float(br["gross"]),
        "total_deduction": float(br["total_deduction"]),
        "net_settlement": float(br["net_to_merchant"]),
        "bittu_fee": float(br["bittu_fee"]),
        "gst_on_fee": float(br["gst_on_fee"]),
        "fee_basis": FEE_BASIS,
        "razorpay_cut": float(br["razorpay_cut"]),
        "platform_pool": float(br["platform_pool"]),
        "platform_margin": float(br["platform_margin"]),
        "platform_loss": float(br["platform_loss"]),
        "payment_kind": br["payment_kind"],
        "gateway_charges_estimated": br["gateway_charges_estimated"],
        "total_deduction_rate": rates["total_deduction_rate_pct"],
        "bittu_fee_rate": rates["bittu_fee_rate_pct"],
        "gst_on_fee_rate": rates["gst_on_fee_rate_pct"],
        "razorpay_cut_rate": rates["razorpay_cut_rate_pct"],
        "gst_rate": rates["gst_rate_pct"],
        "fee_info": rates,
        "settlement_eta": None,
        "note": "Settlement timing is determined by the linked account's Razorpay schedule.",
    }
