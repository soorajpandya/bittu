"""Fee arithmetic regression tests; no database or application bootstrap needed."""
import runpy
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


class FeePolicyTests(unittest.TestCase):
    def setUp(self):
        with patch.dict("os.environ", {"BITTU_RZP_ESTIMATE_RATE": "0.0165"}):
            self.policy = runpy.run_path(str(
                Path(__file__).resolve().parents[1]
                / "app/services/razorpay/fee_policy.py"
            ))

    def test_thousand_rupees_leaves_950_for_merchant(self):
        self.assertEqual(
            self.policy["provisional_merchant_transfer_paise"](100000, "upi"),
            (95000, 3350, 1650),
        )

    def test_total_includes_gst(self):
        self.assertEqual(self.policy["TOTAL_DEDUCTION_RATE"], Decimal("0.05"))
        self.assertAlmostEqual(
            self.policy["PLATFORM_FEE_BASE_RATE"] * (1 + self.policy["GST_RATE"]),
            Decimal("0.05"),
        )

    def test_split_conserves_paise_and_rounds_within_one_paisa(self):
        split = self.policy["provisional_merchant_transfer_paise"]
        for gross in (100, 101, 999, 1000, 10001, 1234567):
            with self.subTest(gross=gross):
                merchant, bittu, gateway = split(gross, "card")
                self.assertEqual(merchant + bittu + gateway, gross)
                self.assertLessEqual(abs(Decimal(merchant) - Decimal(gross) * Decimal("0.95")), 1)

    def test_cash_has_no_gateway_estimate(self):
        for method in ("cash", "counter", "cod"):
            self.assertEqual(self.policy["estimate_rzp_charges_paise"](100000, method), 0)


if __name__ == "__main__":
    unittest.main()
