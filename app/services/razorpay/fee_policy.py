"""Bittu fee policy — the single source of truth for the split.

Only the Bittu platform fee is fixed (1.53% of gross). The Razorpay
charge is NOT assumed — at capture time it is *estimated* (because the
actual fee is not yet known) and later trued-up from the actual values
stored on rzp_payments / rzp_settlements / rzp_route_transfers.

    merchant_settlement = gross - razorpay_total_charges - bittu_fee
    bittu_fee           = gross * 1.53%   (fixed, all methods)
"""
from __future__ import annotations

import os
from decimal import Decimal, ROUND_HALF_UP

# ── The ONLY fixed rate in the system ─────────────────────────────
BITTU_FEE_RATE = Decimal("0.0153")          # 1.53% of gross, all methods

# Provisional Razorpay-charge estimate used ONLY to compute the
# capture-time transfer. Trued-up from actuals at settlement, so an
# imperfect estimate self-corrects and never changes the Bittu margin.
# Tunable via env without code change; NOT a billing assumption.
_DEFAULT_RZP_ESTIMATE_RATE = Decimal(
    os.getenv("BITTU_RZP_ESTIMATE_RATE", "0.0147")  # ~1.47% incl GST
)
_CASH_METHODS = {"cash", "counter", "cod"}


def _q_paise(amount_paise: Decimal | int) -> int:
    """Quantise to whole paise, ROUND_HALF_UP."""
    return int(Decimal(amount_paise).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def bittu_fee_paise(gross_paise: int) -> int:
    """Fixed 1.53% Bittu platform fee, in paise."""
    return _q_paise(Decimal(gross_paise) * BITTU_FEE_RATE)


def estimate_rzp_charges_paise(gross_paise: int, method: str | None = None) -> int:
    """Provisional Razorpay charge (fee + GST) estimate, in paise.

    Cash / counter / COD have no Razorpay charge.
    """
    if method and method.lower() in _CASH_METHODS:
        return 0
    return _q_paise(Decimal(gross_paise) * _DEFAULT_RZP_ESTIMATE_RATE)


def provisional_merchant_transfer_paise(
    gross_paise: int, method: str | None = None
) -> tuple[int, int, int]:
    """Return (merchant_transfer, bittu_fee, est_rzp_charges) in paise.

        merchant_transfer = gross - bittu_fee - est_rzp_charges
    """
    bittu = bittu_fee_paise(gross_paise)
    est_rzp = estimate_rzp_charges_paise(gross_paise, method)
    transfer = gross_paise - bittu - est_rzp
    return transfer, bittu, est_rzp
