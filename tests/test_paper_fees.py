import unittest

from paper_fees import (
    conservative_fee_fraction_of_notional,
    estimate_taker_fee,
    resolve_taker_fee_model,
)


class PaperFeeTests(unittest.TestCase):
    def test_resolves_market_specific_gamma_fee_schedule(self):
        model = resolve_taker_fee_model({
            "feesEnabled": True,
            "feeSchedule": {
                "rate": 0.07, "exponent": 1,
                "takerOnly": True, "rebateRate": 0.2,
            },
        })
        self.assertEqual(model["status"], "known")
        self.assertEqual(model["mode"], "polymarket_taker_curve")
        self.assertEqual(model["rate"], 0.07)
        self.assertEqual(model["source"], "gamma_fee_schedule")

    def test_market_explicitly_fee_free_uses_zero_fee(self):
        model = resolve_taker_fee_model({"feesEnabled": False}, fallback_rate=0.1)
        fee = estimate_taker_fee(model, shares=100, price=0.5, notional=50)
        self.assertEqual(fee["fee"], 0)
        self.assertEqual(model["mode"], "fee_free")

    def test_fee_formula_uses_share_count_and_price_curve(self):
        model = resolve_taker_fee_model({
            "feesEnabled": True, "feeSchedule": {"rate": 0.07, "exponent": 1},
        })
        midpoint = estimate_taker_fee(model, shares=100, price=0.5)
        lower_price = estimate_taker_fee(model, shares=100, price=0.3)
        upper_price = estimate_taker_fee(model, shares=100, price=0.7)
        self.assertAlmostEqual(midpoint["fee"], 1.75)
        self.assertAlmostEqual(lower_price["fee"], 1.47)
        self.assertAlmostEqual(upper_price["fee"], 1.47)

    def test_fee_rounding_uses_five_decimal_precision_and_minimum(self):
        model = resolve_taker_fee_model({
            "feesEnabled": True, "feeSchedule": {"rate": 0.00001, "exponent": 1},
        })
        fee = estimate_taker_fee(model, shares=1, price=0.5)
        self.assertEqual(fee["fee"], 0.00001)
        self.assertEqual(fee["mode"], "polymarket_taker_curve")

    def test_enabled_market_without_schedule_is_unknown_not_silently_free(self):
        model = resolve_taker_fee_model({"feesEnabled": True}, fallback_rate=0.0)
        self.assertEqual(model["status"], "unknown")
        self.assertEqual(model["mode"], "missing_fee_schedule")
        self.assertIsNone(model["rate"])
        self.assertIsNone(estimate_taker_fee(model, shares=10, price=0.5)["fee"])

    def test_unsupported_exponent_is_rejected(self):
        model = resolve_taker_fee_model({
            "feesEnabled": True, "feeSchedule": {"rate": 0.07, "exponent": 2},
        })
        self.assertEqual(model["status"], "unknown")
        self.assertEqual(model["mode"], "unsupported_fee_schedule")

    def test_unknown_metadata_uses_explicit_legacy_flat_fallback(self):
        model = resolve_taker_fee_model({}, fallback_rate=0.01)
        fee = estimate_taker_fee(model, shares=20, price=0.25, notional=5)
        self.assertEqual(model["status"], "assumed")
        self.assertEqual(model["mode"], "legacy_flat_rate_fallback")
        self.assertAlmostEqual(fee["fee"], 0.05)

    def test_cash_reserve_uses_market_fee_curve_maximum_at_low_price(self):
        model = resolve_taker_fee_model({
            "feesEnabled": True, "feeSchedule": {"rate": 0.07, "exponent": 1},
        })
        reserve_at_30c = conservative_fee_fraction_of_notional(model, lowest_price=0.3)
        reserve_at_70c = conservative_fee_fraction_of_notional(model, lowest_price=0.7)
        self.assertAlmostEqual(reserve_at_30c, 0.049)
        self.assertAlmostEqual(reserve_at_70c, 0.021)

    def test_rejects_bad_inputs(self):
        model = resolve_taker_fee_model({
            "feesEnabled": True, "feeSchedule": {"rate": 0.07, "exponent": 1},
        })
        with self.assertRaises(ValueError):
            estimate_taker_fee(model, shares=-1, price=0.5)
        with self.assertRaises(ValueError):
            resolve_taker_fee_model({}, fallback_rate=-0.1)


if __name__ == "__main__":
    unittest.main()
