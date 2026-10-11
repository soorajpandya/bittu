"""
Tax / GST engine — the single source of truth for bill calculations.

All POS, dine-in, QR-table and invoice paths MUST call ``compute_tax`` so
the cart, checkout, kitchen ticket, invoice PDF and printed receipt agree
to the last paisa.

Design notes
------------
* All money is ``Decimal`` internally; results are quantised to 2 dp using
  ``ROUND_HALF_UP`` (matches GST law and what the customer sees).
* ``gst_enabled = false`` zeroes out every tax field — the cart and the
  bill simply show ``Subtotal`` and ``Grand Total``.
* When ``tax_inclusive = true`` the line prices already contain GST, so we
  reverse-derive the taxable amount instead of grossing up.
* The DB tax columns are mirrored on the order row (``cgst_amount``,
  ``sgst_amount``, ``gst_number``) so reprints stay byte-stable even if a
  merchant later changes the rate.

This module deliberately has no async DB calls outside ``get_tax_config``
and ``invalidate_tax_config``, so ``compute_tax`` is trivially testable
and safe to call inside any transaction.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any, Iterable, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


# 60 s TTL: rates are owner-controlled and rarely change but are read on
# every checkout. Avoids one cross-region round-trip per order.
_CONFIG_CACHE: dict[str, tuple["TaxConfig", float]] = {}
_CONFIG_TTL_SEC = 60.0

_TWO_PLACES = Decimal("0.01")
_ZERO = Decimal("0")
_MAX_RATE = Decimal("28")     # India's highest GST slab
_GSTIN_LEN = 15               # GSTIN is fixed 15 chars


def _q(value: Decimal) -> Decimal:
    """Quantise to 2 decimal places."""
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def _to_decimal(value: Any, *, default: Decimal = _ZERO) -> Decimal:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


# ─────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TaxConfig:
    """Effective GST configuration for a restaurant."""
    gst_enabled: bool = True
    gst_type: str = "GST"
    gst_number: Optional[str] = None
    gst_percentage: Decimal = Decimal("5")
    cgst_percentage: Decimal = Decimal("2.5")
    sgst_percentage: Decimal = Decimal("2.5")
    tax_inclusive: bool = False

    @classmethod
    def from_row(cls, row: Any) -> "TaxConfig":
        """Build from an asyncpg Record / dict; tolerant of missing keys."""
        if row is None:
            return cls()
        if isinstance(row, dict):
            get = lambda k, d=None: row.get(k, d)
        else:
            get = lambda k, d=None: (row[k] if k in row else d)
        enabled_raw = get("gst_enabled", None)
        # Legacy rows without the new column: enable when tax_percentage > 0
        if enabled_raw is None:
            enabled = _to_decimal(get("tax_percentage")) > 0
        else:
            enabled = bool(enabled_raw)
        gst_pct = _to_decimal(
            get("gst_percentage"),
            default=_to_decimal(get("tax_percentage"), default=Decimal("5")),
        )
        cgst = _to_decimal(get("cgst_percentage"), default=gst_pct / 2)
        sgst = _to_decimal(get("sgst_percentage"), default=gst_pct / 2)
        gst_no_raw = get("gst_number")
        return cls(
            gst_enabled=enabled,
            gst_type=str(get("gst_type") or "GST"),
            gst_number=(str(gst_no_raw).strip().upper() if gst_no_raw else None),
            gst_percentage=gst_pct,
            cgst_percentage=cgst,
            sgst_percentage=sgst,
            tax_inclusive=bool(get("tax_inclusive", False)),
        )


# ─────────────────────────────────────────────────────────────────
# Breakdown
# ─────────────────────────────────────────────────────────────────
@dataclass
class TaxBreakdown:
    """Result of ``compute_tax`` — exact numbers shown on the receipt."""
    subtotal: Decimal
    discount_amount: Decimal
    taxable_amount: Decimal
    gst_enabled: bool
    gst_number: Optional[str]
    gst_type: str
    gst_percentage: Decimal
    cgst_percentage: Decimal
    sgst_percentage: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    total_tax: Decimal
    grand_total: Decimal
    round_off: Decimal = _ZERO
    tax_inclusive: bool = False

    def to_response(self) -> dict:
        """JSON-safe dict for API responses (floats, not Decimal)."""
        return {
            "subtotal":         float(self.subtotal),
            "discount_amount":  float(self.discount_amount),
            "taxable_amount":   float(self.taxable_amount),
            "gst_enabled":      self.gst_enabled,
            "gst_number":       self.gst_number,
            "gst_type":         self.gst_type,
            "gst_percentage":   float(self.gst_percentage)  if self.gst_enabled else 0.0,
            "cgst_percentage":  float(self.cgst_percentage) if self.gst_enabled else 0.0,
            "sgst_percentage":  float(self.sgst_percentage) if self.gst_enabled else 0.0,
            "cgst_amount":      float(self.cgst_amount),
            "sgst_amount":      float(self.sgst_amount),
            "total_tax":        float(self.total_tax),
            "grand_total":      float(self.grand_total),
            "round_off":        float(self.round_off),
            "tax_inclusive":    self.tax_inclusive,
        }


# ─────────────────────────────────────────────────────────────────
# Core computation
# ─────────────────────────────────────────────────────────────────
def compute_tax(
    subtotal: Any,
    *,
    discount: Any = 0,
    config: TaxConfig,
    round_to_rupee: bool = False,
) -> TaxBreakdown:
    """Compute tax + grand total for a single bill.

    Args:
        subtotal: gross line-item total (before discount).
        discount: order-level discount amount (NOT %).
        config:   effective ``TaxConfig`` for the merchant.
        round_to_rupee: if True, grand_total is rounded to nearest rupee
                       and the delta is captured as ``round_off``.

    Returns:
        ``TaxBreakdown`` with every field needed by the cart, checkout,
        invoice and receipt.
    """
    sub = max(_to_decimal(subtotal), _ZERO)
    disc = max(_to_decimal(discount), _ZERO)
    if disc > sub:
        disc = sub                       # never refund-via-discount

    if not config.gst_enabled:
        taxable = _q(sub - disc)
        return TaxBreakdown(
            subtotal=_q(sub), discount_amount=_q(disc),
            taxable_amount=taxable,
            gst_enabled=False, gst_number=None, gst_type=config.gst_type,
            gst_percentage=_ZERO, cgst_percentage=_ZERO, sgst_percentage=_ZERO,
            cgst_amount=_ZERO, sgst_amount=_ZERO, total_tax=_ZERO,
            grand_total=taxable, tax_inclusive=False,
        )

    cgst_pct = max(config.cgst_percentage, _ZERO)
    sgst_pct = max(config.sgst_percentage, _ZERO)
    gst_pct  = cgst_pct + sgst_pct      # derive total from parts so split is canonical

    if config.tax_inclusive:
        # Reverse-derive taxable: gross / (1 + gst_pct/100)
        gross = sub - disc
        divisor = (Decimal("100") + gst_pct) / Decimal("100")
        taxable = gross / divisor if divisor != 0 else gross
    else:
        taxable = sub - disc

    cgst_amount = _q(taxable * cgst_pct / Decimal("100"))
    sgst_amount = _q(taxable * sgst_pct / Decimal("100"))
    total_tax = cgst_amount + sgst_amount

    if config.tax_inclusive:
        grand = _q(sub - disc)           # already includes tax
    else:
        grand = _q(taxable + total_tax)

    round_off = _ZERO
    if round_to_rupee:
        rounded = grand.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        round_off = rounded - grand
        grand = rounded

    return TaxBreakdown(
        subtotal=_q(sub),
        discount_amount=_q(disc),
        taxable_amount=_q(taxable),
        gst_enabled=True,
        gst_number=config.gst_number,
        gst_type=config.gst_type,
        gst_percentage=_q(gst_pct),
        cgst_percentage=_q(cgst_pct),
        sgst_percentage=_q(sgst_pct),
        cgst_amount=cgst_amount,
        sgst_amount=sgst_amount,
        total_tax=_q(total_tax),
        grand_total=grand,
        round_off=_q(round_off),
        tax_inclusive=config.tax_inclusive,
    )


# ─────────────────────────────────────────────────────────────────
# Item-level (cart-aware) tax engine
# ─────────────────────────────────────────────────────────────────
#
# Real restaurants sell a mix of:
#   * EXCLUSIVE items  — restaurant-made food; GST is added on top
#                        of the selling price.        tax = price * rate
#   * INCLUSIVE items  — packaged MRP goods (Coke, Thums Up, water,
#                        chips). The printed MRP already includes GST,
#                        so we MUST NOT charge it again — instead we
#                        reverse-derive the taxable portion for the
#                        government's books.          taxable = price / (1 + rate)
#                                                     gst     = price - taxable
#   * NON-GST items    — items the merchant has explicitly flagged as
#                        gst_enabled = false.         tax = 0
#
# ``compute_cart_tax`` processes a list of ``ItemTaxLine`` and produces
# a ``CartTaxBreakdown`` whose totals match ``TaxBreakdown`` plus a
# ``lines`` list for invoice / receipt rendering.
#
# Discount policy (single policy shared with ``compute_tax``): an order-level
# discount reduces the *taxable value*. It is allocated pro-rata across lines
# on their subtotal basis (pre-tax for exclusive lines, MRP for inclusive
# lines, price for non-GST lines), then each line's GST is re-derived on the
# discounted basis — exclusive: tax on top of the reduced price; inclusive:
# reverse-derive from the reduced MRP. GST law: a discount shown on the
# invoice reduces the value of supply, so tax is never charged on the
# discounted portion. Odd tax paise go to SGST so CGST == SGST otherwise.

@dataclass(frozen=True)
class ItemTaxLine:
    """One cart/order line as fed into ``compute_cart_tax``.

    ``line_total`` is the customer-facing line subtotal:
      * exclusive items: pre-tax (tax will be added on top)
      * inclusive items: MRP × qty (already includes tax)
    """
    line_total: Decimal
    gst_enabled: bool = True
    gst_inclusive: bool = False
    gst_rate: Decimal = Decimal("0")     # total %; split 50/50 into CGST/SGST


@dataclass
class LineTaxResult:
    """Per-line breakdown — what the receipt prints next to each item."""
    line_total: Decimal          # input price (unchanged)
    gst_enabled: bool
    gst_inclusive: bool
    gst_rate: Decimal
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    total_tax: Decimal
    final_price: Decimal         # what the customer pays for this line (after discount)
    discount_amount: Decimal = _ZERO   # share of the order discount allocated here

    def to_response(self) -> dict:
        return {
            "line_total":      float(self.line_total),
            "discount_amount": float(self.discount_amount),
            "gst_enabled":     self.gst_enabled,
            "gst_inclusive":   self.gst_inclusive,
            "gst_percentage":  float(self.gst_rate),
            "taxable_amount":  float(self.taxable_amount),
            "cgst_amount":     float(self.cgst_amount),
            "sgst_amount":     float(self.sgst_amount),
            "total_tax":       float(self.total_tax),
            "final_price":     float(self.final_price),
        }


@dataclass
class CartTaxBreakdown:
    """Aggregate result of ``compute_cart_tax`` — every number the
    bill, invoice and printed receipt need, plus the per-line list."""
    subtotal: Decimal                 # sum of raw line_total
    inclusive_subtotal: Decimal       # MRP items (already include GST)
    exclusive_subtotal: Decimal       # pre-tax of GST-on-top items
    nongst_subtotal: Decimal          # gst_enabled=false items
    discount_amount: Decimal
    taxable_amount: Decimal           # exclusive_taxable + inclusive_taxable
    cgst_amount: Decimal
    sgst_amount: Decimal
    total_tax: Decimal
    grand_total: Decimal
    round_off: Decimal
    gst_enabled: bool
    gst_number: Optional[str]
    gst_type: str
    lines: list[LineTaxResult] = field(default_factory=list)

    def to_response(self) -> dict:
        return {
            "subtotal":           float(self.subtotal),
            "inclusive_subtotal": float(self.inclusive_subtotal),
            "exclusive_subtotal": float(self.exclusive_subtotal),
            "nongst_subtotal":    float(self.nongst_subtotal),
            "discount_amount":    float(self.discount_amount),
            "taxable_amount":     float(self.taxable_amount),
            "gst_enabled":        self.gst_enabled,
            "gst_number":         self.gst_number,
            "gst_type":           self.gst_type,
            "cgst_amount":        float(self.cgst_amount),
            "sgst_amount":        float(self.sgst_amount),
            "total_tax":          float(self.total_tax),
            "grand_total":        float(self.grand_total),
            "round_off":          float(self.round_off),
            "lines":              [l.to_response() for l in self.lines],
        }


def _line_from_item_row(row: Any, line_total: Any, *, store: TaxConfig) -> ItemTaxLine:
    """Build an ``ItemTaxLine`` from an ``items`` row + computed line_total.

    Falls back to the store-level GST config when the item row is missing
    fields (e.g. legacy items added before M058).
    """
    def _get(key, default=None):
        if row is None:
            return default
        if isinstance(row, dict):
            return row.get(key, default)
        try:
            return row[key] if key in row else default
        except (KeyError, TypeError):
            return default

    # Per-item override of "is this item GST'd at all?"
    # M058 doesn't have a per-item enable flag, so we infer:
    #   gst_rate > 0  AND  store.gst_enabled  ⇒  enabled.
    rate = _to_decimal(_get("gst_rate"), default=store.gst_percentage)
    enabled = store.gst_enabled and rate > 0
    inclusive = bool(_get("is_tax_inclusive", store.tax_inclusive))

    return ItemTaxLine(
        line_total=_to_decimal(line_total),
        gst_enabled=enabled,
        gst_inclusive=inclusive,
        gst_rate=rate,
    )


def _split_tax(tax: Decimal) -> tuple[Decimal, Decimal]:
    """Split quantised tax into (cgst, sgst); an odd paisa goes to SGST."""
    tax_q = _q(tax)
    paise = int((tax_q * 100).to_integral_value(rounding=ROUND_HALF_UP))
    cgst = Decimal(paise // 2) / 100
    return _q(cgst), _q(tax_q - cgst)


def _allocate_discount(amounts: list[Decimal], discount: Decimal) -> list[Decimal]:
    """Pro-rata paise allocation of `discount` over `amounts`; conserves the
    total exactly (remainder goes to the last non-zero line)."""
    total = sum(amounts, _ZERO)
    if discount <= 0 or total <= 0:
        return [_ZERO for _ in amounts]
    disc_paise = int((_q(discount) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    total_paise = int((_q(total) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    shares: list[int] = []
    for amt in amounts:
        amt_paise = int((_q(amt) * 100).to_integral_value(rounding=ROUND_HALF_UP))
        shares.append(disc_paise * amt_paise // total_paise)
    remainder = disc_paise - sum(shares)
    for i in range(len(shares) - 1, -1, -1):
        if remainder == 0:
            break
        if amounts[i] > 0:
            room = int((_q(amounts[i]) * 100).to_integral_value(rounding=ROUND_HALF_UP)) - shares[i]
            take = min(room, remainder)
            shares[i] += take
            remainder -= take
    return [Decimal(s) / 100 for s in shares]


def compute_cart_tax(
    lines: Iterable[ItemTaxLine],
    *,
    discount: Any = 0,
    config: TaxConfig,
    round_to_rupee: bool = False,
) -> CartTaxBreakdown:
    """Compute item-level GST for a mixed cart.

    The order-level discount reduces each line's taxable basis pro-rata
    (see module comment); GST is then computed on the discounted basis.
    Invariants: grand_total == Σ final_price ± round_off
                grand_total == subtotal − discount + Σ exclusive tax ± round_off
                cgst == sgst except for an odd paisa, which lands on SGST.
    """
    lines = list(lines)
    store_enabled = bool(config.gst_enabled)
    bases = [max(_to_decimal(l.line_total), _ZERO) for l in lines]
    subtotal = sum(bases, _ZERO)

    disc = max(_to_decimal(discount), _ZERO)
    if disc > subtotal:
        disc = subtotal
    disc = _q(disc)
    allocations = _allocate_discount(bases, disc)

    line_results: list[LineTaxResult] = []
    inclusive_sub = exclusive_sub = nongst_sub = _ZERO
    cgst_total = sgst_total = exclusive_tax = taxable_total = _ZERO
    grand = _ZERO

    for line, amt, d in zip(lines, bases, allocations):
        rate = max(_to_decimal(line.gst_rate), _ZERO)
        if rate > _MAX_RATE:
            rate = _MAX_RATE
        enabled = bool(line.gst_enabled) and store_enabled and rate > 0
        basis = _q(amt - d)                      # discounted subtotal basis

        if not enabled:
            nongst_sub += amt
            taxable_q = basis
            cgst = sgst = _ZERO
            final = basis
            res_inclusive = False
        elif line.gst_inclusive:
            # MRP already includes GST — reduce the MRP, then reverse-derive.
            divisor = (Decimal("100") + rate) / Decimal("100")
            taxable_raw = basis / divisor
            taxable_q = _q(taxable_raw)
            cgst, sgst = _split_tax(basis - taxable_q)
            final = basis
            inclusive_sub += amt
            res_inclusive = True
        else:
            # GST on top of the discounted price.
            taxable_q = basis
            cgst, sgst = _split_tax(basis * rate / Decimal("100"))
            final = _q(basis + cgst + sgst)
            exclusive_sub += amt
            exclusive_tax += cgst + sgst
            res_inclusive = False

        if enabled:
            taxable_total += taxable_q
            cgst_total += cgst
            sgst_total += sgst
        grand += final
        line_results.append(LineTaxResult(
            line_total=_q(amt), gst_enabled=enabled, gst_inclusive=res_inclusive,
            gst_rate=_q(rate) if enabled else _ZERO, taxable_amount=taxable_q,
            cgst_amount=cgst, sgst_amount=sgst, total_tax=_q(cgst + sgst),
            final_price=final, discount_amount=_q(d),
        ))

    grand = _q(grand)
    round_off = _ZERO
    if round_to_rupee:
        rounded = grand.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        round_off = rounded - grand
        grand = rounded

    return CartTaxBreakdown(
        subtotal=_q(subtotal),
        inclusive_subtotal=_q(inclusive_sub),
        exclusive_subtotal=_q(exclusive_sub),
        nongst_subtotal=_q(nongst_sub),
        discount_amount=disc,
        taxable_amount=_q(taxable_total),
        cgst_amount=_q(cgst_total),
        sgst_amount=_q(sgst_total),
        total_tax=_q(cgst_total + sgst_total),
        grand_total=grand,
        round_off=_q(round_off),
        gst_enabled=store_enabled and (cgst_total + sgst_total) > 0,
        gst_number=config.gst_number if store_enabled else None,
        gst_type=config.gst_type,
        lines=line_results,
    )


# ─────────────────────────────────────────────────────────────────
# DB lookup with TTL cache
# ─────────────────────────────────────────────────────────────────
async def get_tax_config(conn, restaurant_id: Optional[str]) -> TaxConfig:
    """Return effective ``TaxConfig`` for a restaurant (cached 60 s)."""
    if not restaurant_id:
        return TaxConfig()
    key = str(restaurant_id)
    cached = _CONFIG_CACHE.get(key)
    now = time.monotonic()
    if cached and now - cached[1] < _CONFIG_TTL_SEC:
        return cached[0]

    row = await conn.fetchrow(
        """
        SELECT rs.gst_enabled, rs.gst_type, rs.gst_number,
               rs.gst_percentage, rs.cgst_percentage, rs.sgst_percentage,
               rs.tax_inclusive, rs.tax_percentage
          FROM restaurant_settings rs
          LEFT JOIN restaurants r ON r.id = $1::uuid
         WHERE rs.restaurant_id = $1::uuid
            OR (
                 rs.restaurant_id IS NULL
                 AND r.owner_id::text = rs.user_id::text
               )
         ORDER BY
               CASE WHEN rs.restaurant_id = $1::uuid THEN 0 ELSE 1 END,
               rs.updated_at DESC NULLS LAST,
               rs.id DESC
         LIMIT 1
        """,
        restaurant_id,
    )
    cfg = TaxConfig.from_row(row)
    _CONFIG_CACHE[key] = (cfg, now)
    return cfg


def invalidate_tax_config(restaurant_id: Optional[str]) -> None:
    """Drop cached config so the next read picks up settings changes."""
    if not restaurant_id:
        return
    _CONFIG_CACHE.pop(str(restaurant_id), None)


# ─────────────────────────────────────────────────────────────────
# Validation helpers (used by the settings endpoint)
# ─────────────────────────────────────────────────────────────────
class TaxConfigError(ValueError):
    """Raised when a user-supplied GST config is invalid."""


def validate_gst_number(value: Optional[str]) -> Optional[str]:
    """Light-weight GSTIN check: exactly 15 alphanumeric characters.

    Deliberately doesn't validate the GSTIN checksum — heavier validation
    belongs in a KYC service. Front office may type test numbers during
    onboarding.
    """
    if value is None or str(value).strip() == "":
        return None
    norm = str(value).strip().upper()
    if len(norm) != _GSTIN_LEN or not norm.isalnum():
        raise TaxConfigError("gst_number must be 15 alphanumeric characters")
    return norm


def validate_gst_settings_patch(patch: dict, *, existing_gst_number: Optional[str] = None) -> dict:
    """Validate / normalise the GST-related fields in an update payload.

    Returns the cleaned dict (with normalised gst_number, derived rates)
    or raises ``TaxConfigError``. ``existing_gst_number`` is the value
    already stored so callers don't have to resend it on every PUT.
    """
    out = dict(patch)

    if "gst_number" in out:
        out["gst_number"] = validate_gst_number(out["gst_number"])

    for k in ("gst_percentage", "cgst_percentage", "sgst_percentage"):
        if k in out and out[k] is not None:
            v = _to_decimal(out[k])
            if v < 0 or v > _MAX_RATE:
                raise TaxConfigError(f"{k} must be between 0 and {int(_MAX_RATE)}")
            out[k] = float(v)

    gst_pct  = out.get("gst_percentage")
    cgst_pct = out.get("cgst_percentage")
    sgst_pct = out.get("sgst_percentage")

    # If only the total rate was supplied, derive a 50/50 split.
    if gst_pct is not None and cgst_pct is None and sgst_pct is None:
        half = _q(_to_decimal(gst_pct) / 2)
        out["cgst_percentage"] = float(half)
        out["sgst_percentage"] = float(_to_decimal(gst_pct) - half)
    # If a split was supplied without a total, sum them.
    elif gst_pct is None and (cgst_pct is not None or sgst_pct is not None):
        out["gst_percentage"] = float(
            _to_decimal(cgst_pct) + _to_decimal(sgst_pct)
        )

    # Cross-check: CGST + SGST must equal GST total (within 0.01).
    if (
        out.get("gst_percentage")  is not None
        and out.get("cgst_percentage") is not None
        and out.get("sgst_percentage") is not None
    ):
        total = _to_decimal(out["gst_percentage"])
        parts = _to_decimal(out["cgst_percentage"]) + _to_decimal(out["sgst_percentage"])
        if abs(total - parts) > Decimal("0.01"):
            raise TaxConfigError(
                "cgst_percentage + sgst_percentage must equal gst_percentage"
            )

    # When enabling GST, a GSTIN must already exist or be supplied now.
    if out.get("gst_enabled") is True:
        effective = out.get("gst_number") if "gst_number" in out else existing_gst_number
        if not effective:
            raise TaxConfigError("gst_number is required when gst_enabled is true")

    return out
