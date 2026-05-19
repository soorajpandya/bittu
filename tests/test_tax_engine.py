"""Unit tests for app.services.tax_engine."""
from decimal import Decimal

import pytest

from app.services.tax_engine import (
    TaxConfig, TaxConfigError, compute_tax,
    ItemTaxLine, compute_cart_tax, _line_from_item_row,
    validate_gst_number, validate_gst_settings_patch,
)


# ── TaxConfig.from_row ───────────────────────────────────────────
def test_from_row_legacy_no_gst_enabled_column():
    cfg = TaxConfig.from_row({"tax_percentage": "5"})
    assert cfg.gst_enabled is True
    assert cfg.gst_percentage == Decimal("5")
    # legacy rows have no split → default 50/50
    assert cfg.cgst_percentage == Decimal("2.5")
    assert cfg.sgst_percentage == Decimal("2.5")


def test_from_row_legacy_disabled_when_tax_zero():
    cfg = TaxConfig.from_row({"tax_percentage": "0"})
    assert cfg.gst_enabled is False


def test_from_row_none():
    cfg = TaxConfig.from_row(None)
    assert cfg.gst_enabled is True
    assert cfg.gst_percentage == Decimal("5")


def test_from_row_full():
    cfg = TaxConfig.from_row({
        "gst_enabled": True, "gst_type": "GST",
        "gst_number": "29ABCDE1234F1Z5",
        "gst_percentage": "5", "cgst_percentage": "2.5",
        "sgst_percentage": "2.5", "tax_inclusive": False,
        "tax_percentage": "5",
    })
    assert cfg.gst_number == "29ABCDE1234F1Z5"
    assert cfg.gst_enabled is True


# ── compute_tax ──────────────────────────────────────────────────
def test_compute_disabled_zeroes_everything():
    cfg = TaxConfig(gst_enabled=False)
    b = compute_tax("100", config=cfg)
    assert b.total_tax == Decimal("0")
    assert b.cgst_amount == Decimal("0")
    assert b.sgst_amount == Decimal("0")
    assert b.grand_total == Decimal("100.00")
    assert b.gst_enabled is False


def test_compute_5pct_split():
    cfg = TaxConfig(
        gst_enabled=True, gst_percentage=Decimal("5"),
        cgst_percentage=Decimal("2.5"), sgst_percentage=Decimal("2.5"),
        gst_number="29ABCDE1234F1Z5",
    )
    b = compute_tax("1000", config=cfg)
    assert b.cgst_amount == Decimal("25.00")
    assert b.sgst_amount == Decimal("25.00")
    assert b.total_tax == Decimal("50.00")
    assert b.grand_total == Decimal("1050.00")


def test_compute_with_discount():
    cfg = TaxConfig(
        gst_enabled=True, gst_percentage=Decimal("5"),
        cgst_percentage=Decimal("2.5"), sgst_percentage=Decimal("2.5"),
        gst_number="29ABCDE1234F1Z5",
    )
    b = compute_tax("1000", discount="100", config=cfg)
    # tax is on (1000 - 100) = 900
    assert b.taxable_amount == Decimal("900.00")
    assert b.total_tax == Decimal("45.00")
    assert b.grand_total == Decimal("945.00")


def test_compute_discount_capped_at_subtotal():
    cfg = TaxConfig(gst_enabled=False)
    b = compute_tax("100", discount="500", config=cfg)
    assert b.discount_amount == Decimal("100.00")
    assert b.grand_total == Decimal("0.00")


def test_compute_tax_inclusive():
    # 1050 inclusive of 5% GST → taxable 1000, tax 50
    cfg = TaxConfig(
        gst_enabled=True, gst_percentage=Decimal("5"),
        cgst_percentage=Decimal("2.5"), sgst_percentage=Decimal("2.5"),
        gst_number="29ABCDE1234F1Z5", tax_inclusive=True,
    )
    b = compute_tax("1050", config=cfg)
    assert b.taxable_amount == Decimal("1000.00")
    assert b.total_tax == Decimal("50.00")
    assert b.grand_total == Decimal("1050.00")


def test_compute_round_to_rupee_captures_delta():
    cfg = TaxConfig(
        gst_enabled=True, gst_percentage=Decimal("5"),
        cgst_percentage=Decimal("2.5"), sgst_percentage=Decimal("2.5"),
        gst_number="29ABCDE1234F1Z5",
    )
    # 199 → CGST 4.98 + SGST 4.98 = 9.96, grand 208.96 → rounds to 209.00,
    # round_off = +0.04
    b = compute_tax("199", config=cfg, round_to_rupee=True)
    assert b.grand_total == Decimal("209.00")
    assert b.round_off == Decimal("0.04")


def test_compute_idempotent():
    cfg = TaxConfig(
        gst_enabled=True, gst_percentage=Decimal("5"),
        cgst_percentage=Decimal("2.5"), sgst_percentage=Decimal("2.5"),
        gst_number="29ABCDE1234F1Z5",
    )
    a = compute_tax("123.45", discount="5", config=cfg)
    b = compute_tax("123.45", discount="5", config=cfg)
    assert a.to_response() == b.to_response()


def test_compute_response_shape():
    cfg = TaxConfig(
        gst_enabled=True, gst_percentage=Decimal("5"),
        cgst_percentage=Decimal("2.5"), sgst_percentage=Decimal("2.5"),
        gst_number="29ABCDE1234F1Z5",
    )
    out = compute_tax("100", config=cfg).to_response()
    for key in (
        "subtotal", "cgst_percentage", "sgst_percentage",
        "cgst_amount", "sgst_amount", "total_tax",
        "grand_total", "gst_enabled", "gst_number",
    ):
        assert key in out


# ── validate_gst_number ──────────────────────────────────────────
def test_validate_gstin_valid():
    assert validate_gst_number("29ABCDE1234F1Z5") == "29ABCDE1234F1Z5"


def test_validate_gstin_normalised_to_upper():
    assert validate_gst_number("29abcde1234f1z5") == "29ABCDE1234F1Z5"


def test_validate_gstin_none_or_empty_passes_through():
    assert validate_gst_number(None) is None
    assert validate_gst_number("") is None
    assert validate_gst_number("   ") is None


def test_validate_gstin_wrong_length():
    with pytest.raises(TaxConfigError):
        validate_gst_number("ABC123")


def test_validate_gstin_special_chars():
    with pytest.raises(TaxConfigError):
        validate_gst_number("29ABCDE1234F1Z-")


# ── compute_cart_tax (item-level GST) ────────────────────────────
#
# Store-level config used across tests: GST 5% enabled (CGST 2.5 + SGST 2.5).
STORE_5 = TaxConfig(
    gst_enabled=True, gst_number="29ABCDE1234F1Z5",
    gst_percentage=Decimal("5"),
    cgst_percentage=Decimal("2.5"), sgst_percentage=Decimal("2.5"),
    tax_inclusive=False,
)


def _line(amount, *, inclusive=False, enabled=True, rate="5"):
    return ItemTaxLine(
        line_total=Decimal(str(amount)),
        gst_enabled=enabled, gst_inclusive=inclusive,
        gst_rate=Decimal(str(rate)),
    )


def test_cart_inclusive_only_thumbs_up():
    """MRP item: ₹40 already includes 5% GST → customer pays ₹40, not ₹42."""
    res = compute_cart_tax([_line(40, inclusive=True)], config=STORE_5)
    assert res.grand_total == Decimal("40.00")
    assert res.inclusive_subtotal == Decimal("40.00")
    assert res.exclusive_subtotal == Decimal("0.00")
    # 40 / 1.05 = 38.0952…
    assert res.taxable_amount == Decimal("38.10")
    assert res.cgst_amount == Decimal("0.95")
    assert res.sgst_amount == Decimal("0.95")
    assert res.lines[0].final_price == Decimal("40.00")


def test_cart_exclusive_only_burger():
    """Restaurant-made item: ₹100 + 5% GST → customer pays ₹105."""
    res = compute_cart_tax([_line(100)], config=STORE_5)
    assert res.grand_total == Decimal("105.00")
    assert res.exclusive_subtotal == Decimal("100.00")
    assert res.taxable_amount == Decimal("100.00")
    assert res.cgst_amount == Decimal("2.50")
    assert res.sgst_amount == Decimal("2.50")
    assert res.lines[0].final_price == Decimal("105.00")


def test_cart_mixed_burger_thumbs_up_water():
    """Burger 100 excl + Thumbs Up 40 incl + Water 20 non-GST."""
    lines = [
        _line(100),                                # exclusive 5%
        _line(40, inclusive=True),                 # inclusive 5%
        _line(20, enabled=False, rate="0"),        # non-GST
    ]
    res = compute_cart_tax(lines, config=STORE_5)
    # 105 (burger) + 40 (mrp) + 20 (water)
    assert res.grand_total == Decimal("165.00")
    assert res.cgst_amount == Decimal("2.50") + Decimal("0.95")   # 3.45
    assert res.sgst_amount == Decimal("2.50") + Decimal("0.95")   # 3.45
    assert res.nongst_subtotal == Decimal("20.00")
    assert res.inclusive_subtotal == Decimal("40.00")
    assert res.exclusive_subtotal == Decimal("100.00")


def test_cart_store_gst_disabled_zeroes_everything():
    """Store-level switch off ⇒ no GST regardless of item flags."""
    off = TaxConfig(gst_enabled=False)
    lines = [_line(100), _line(40, inclusive=True)]
    res = compute_cart_tax(lines, config=off)
    assert res.cgst_amount == Decimal("0.00")
    assert res.sgst_amount == Decimal("0.00")
    # Inclusive line is no longer treated as MRP-with-GST (since GST is off)
    # but the customer still pays the printed price.
    assert res.grand_total == Decimal("140.00")
    assert res.gst_enabled is False


def test_cart_discount_applied_after_tax():
    """₹10 discount on (burger 100 + thumbs up 40) → 145 - 10 = 135."""
    lines = [_line(100), _line(40, inclusive=True)]
    res = compute_cart_tax(lines, discount=10, config=STORE_5)
    assert res.grand_total == Decimal("135.00")
    # Per-line tax is unchanged — discount doesn't rewrite the GST split.
    assert res.cgst_amount == Decimal("3.45")
    assert res.sgst_amount == Decimal("3.45")
    assert res.discount_amount == Decimal("10.00")


def test_cart_discount_cannot_exceed_total():
    res = compute_cart_tax([_line(40, inclusive=True)], discount=500, config=STORE_5)
    assert res.grand_total == Decimal("0.00")
    assert res.discount_amount == Decimal("40.00")


def test_cart_round_off_to_rupee():
    """Round 40.00 grand to 40 → round_off 0; 105 stays 105."""
    res = compute_cart_tax(
        [_line(100), _line(40, inclusive=True)],
        config=STORE_5, round_to_rupee=True,
    )
    # 100*1.05 + 40 = 145.00 — already integer
    assert res.grand_total == Decimal("145")
    assert res.round_off == Decimal("0.00")


def test_cart_inclusive_with_item_disabled_keeps_price():
    """Inclusive item with gst_enabled=false ⇒ no tax extracted."""
    res = compute_cart_tax(
        [_line(40, inclusive=True, enabled=False, rate="0")],
        config=STORE_5,
    )
    assert res.cgst_amount == Decimal("0.00")
    assert res.grand_total == Decimal("40.00")
    assert res.nongst_subtotal == Decimal("40.00")


def test_cart_zero_rate_treated_as_nongst():
    """rate=0 short-circuits even if enabled flag is true."""
    res = compute_cart_tax([_line(40, rate="0")], config=STORE_5)
    assert res.total_tax == Decimal("0.00")
    assert res.grand_total == Decimal("40.00")


def test_cart_empty():
    res = compute_cart_tax([], config=STORE_5)
    assert res.grand_total == Decimal("0.00")
    assert res.lines == []


def test_cart_18pct_inclusive():
    """Different rate: ₹118 MRP @ 18% → taxable 100, GST 18 (9+9)."""
    res = compute_cart_tax(
        [_line(118, inclusive=True, rate="18")],
        config=TaxConfig(gst_enabled=True, gst_percentage=Decimal("18"),
                         cgst_percentage=Decimal("9"), sgst_percentage=Decimal("9")),
    )
    assert res.taxable_amount == Decimal("100.00")
    assert res.cgst_amount == Decimal("9.00")
    assert res.sgst_amount == Decimal("9.00")
    assert res.grand_total == Decimal("118.00")


def test_line_from_item_row_inclusive_mrp():
    row = {
        "gst_rate": Decimal("5"),
        "is_tax_inclusive": True,
        "pricing_type": "mrp",
    }
    line = _line_from_item_row(row, Decimal("40"), store=STORE_5)
    assert line.gst_inclusive is True
    assert line.gst_enabled is True
    assert line.gst_rate == Decimal("5")


def test_line_from_item_row_missing_falls_back_to_store():
    line = _line_from_item_row(None, Decimal("100"), store=STORE_5)
    assert line.gst_enabled is True
    assert line.gst_rate == Decimal("5")
    assert line.gst_inclusive is False


def test_line_from_item_row_zero_rate_disables():
    """gst_rate=0 on the item ⇒ line is non-GST even if store enabled."""
    row = {"gst_rate": Decimal("0"), "is_tax_inclusive": False}
    line = _line_from_item_row(row, Decimal("50"), store=STORE_5)
    assert line.gst_enabled is False


def test_cart_per_line_response_shape():
    """Ensure to_response() yields the API contract from spec §6."""
    res = compute_cart_tax(
        [_line(100), _line(40, inclusive=True)],
        config=STORE_5,
    )
    payload = res.to_response()
    assert {"subtotal", "inclusive_subtotal", "exclusive_subtotal",
            "cgst_amount", "sgst_amount", "grand_total", "lines"} <= payload.keys()
    assert {"line_total", "gst_enabled", "gst_inclusive", "gst_percentage",
            "taxable_amount", "cgst_amount", "sgst_amount",
            "final_price"} <= payload["lines"][0].keys()



# ── validate_gst_settings_patch ──────────────────────────────────
def test_patch_derives_split_from_total():
    out = validate_gst_settings_patch(
        {"gst_percentage": 12}, existing_gst_number="29ABCDE1234F1Z5",
    )
    assert out["cgst_percentage"] == 6.0
    assert out["sgst_percentage"] == 6.0


def test_patch_derives_total_from_split():
    out = validate_gst_settings_patch(
        {"cgst_percentage": 9, "sgst_percentage": 9},
        existing_gst_number="29ABCDE1234F1Z5",
    )
    assert out["gst_percentage"] == 18.0


def test_patch_mismatch_raises():
    with pytest.raises(TaxConfigError):
        validate_gst_settings_patch({
            "gst_percentage": 5,
            "cgst_percentage": 3,
            "sgst_percentage": 3,
        }, existing_gst_number="29ABCDE1234F1Z5")


def test_patch_rate_over_28_raises():
    with pytest.raises(TaxConfigError):
        validate_gst_settings_patch({"gst_percentage": 35})


def test_patch_enabling_without_gst_number_raises():
    with pytest.raises(TaxConfigError):
        validate_gst_settings_patch({"gst_enabled": True})


def test_patch_enabling_with_existing_gst_number_ok():
    out = validate_gst_settings_patch(
        {"gst_enabled": True}, existing_gst_number="29ABCDE1234F1Z5",
    )
    assert out["gst_enabled"] is True


def test_patch_normalises_gstin_case():
    out = validate_gst_settings_patch({
        "gst_enabled": True, "gst_number": "29abcde1234f1z5",
    })
    assert out["gst_number"] == "29ABCDE1234F1Z5"


