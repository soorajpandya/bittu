"""Unit tests for app.services.tax_engine."""
from decimal import Decimal

import pytest

from app.services.tax_engine import (
    TaxConfig, TaxConfigError, compute_tax,
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
