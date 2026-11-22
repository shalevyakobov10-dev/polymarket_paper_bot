from decimal import Decimal
import unittest

from perps_risk import PerpsRiskLimits, evaluate_perps_entry


def snapshot(**overrides):
    value = {
        "research_only": True,
        "execution": "none",
        "entry_eligible": True,
        "close_only": False,
        "settlement": False,
        "ticker_age_seconds": 1,
        "book_age_seconds": 1,
        "mark_price": "100",
        "index_price": "100",
        "spread_bps": "5",
        "min_notional": "1",
        "max_market_notional": "100000",
        "max_leverage": 10,
        "risk_tiers": [{"lower_bound": "0", "max_leverage": 10}],
    }
    value.update(overrides)
    return value


class PerpsRiskTests(unittest.TestCase):
    def test_approves_small_two_x_paper_position(self):
        result = evaluate_perps_entry(
            snapshot(), side="LONG", requested_notional="10",
            equity="100", free_collateral="100",
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["leverage"], "2")
        self.assertEqual(result["initial_margin"], "5")
        self.assertEqual(result["execution"], "none")

    def test_rejects_stale_market_data(self):
        result = evaluate_perps_entry(
            snapshot(ticker_age_seconds=11), side="LONG",
            requested_notional="10", equity="100", free_collateral="100",
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "stale_market_data")

    def test_rejects_close_only_and_settled_instruments(self):
        for updates, expected in (
            ({"close_only": True, "entry_eligible": False}, "instrument_close_only"),
            ({"settlement": True}, "instrument_settled"),
        ):
            result = evaluate_perps_entry(
                snapshot(**updates), side="LONG", requested_notional="10",
                equity="100", free_collateral="100",
            )
            self.assertEqual(result["reason"], expected)

    def test_rejects_leverage_above_tier(self):
        result = evaluate_perps_entry(
            snapshot(risk_tiers=[{"lower_bound": "0", "max_leverage": 1}]),
            side="LONG", requested_notional="10", equity="100", free_collateral="100",
        )
        self.assertEqual(result["reason"], "leverage_exceeds_market_tier")

    def test_rejects_missing_tier_schedule(self):
        result = evaluate_perps_entry(
            snapshot(risk_tiers=[]), side="LONG", requested_notional="10",
            equity="100", free_collateral="100",
        )
        self.assertEqual(result["reason"], "invalid_or_incomplete_risk_input")

    def test_rejects_excessive_risk_budget(self):
        limits = PerpsRiskLimits(risk_per_trade_pct=Decimal("0.001"))
        result = evaluate_perps_entry(
            snapshot(), side="LONG", requested_notional="10",
            equity="100", free_collateral="100", limits=limits,
        )
        self.assertEqual(result["reason"], "risk_per_trade_exceeded")

    def test_rejects_notional_and_portfolio_exposure_limits(self):
        too_large = evaluate_perps_entry(
            snapshot(), side="LONG", requested_notional="21",
            equity="100", free_collateral="100",
        )
        too_much_exposure = evaluate_perps_entry(
            snapshot(), side="SHORT", requested_notional="10",
            equity="100", free_collateral="100", open_exposure="15",
        )
        self.assertEqual(too_large["reason"], "max_position_notional_exceeded")
        self.assertEqual(too_much_exposure["reason"], "max_total_exposure_exceeded")

    def test_rejects_insufficient_collateral_daily_loss_and_drawdown(self):
        low_collateral = evaluate_perps_entry(
            snapshot(), side="LONG", requested_notional="10",
            equity="100", free_collateral="1",
        )
        daily = evaluate_perps_entry(
            snapshot(), side="LONG", requested_notional="10",
            equity="100", free_collateral="100", daily_loss="2",
        )
        drawdown = evaluate_perps_entry(
            snapshot(), side="LONG", requested_notional="10",
            equity="94", peak_equity="100", free_collateral="100",
        )
        self.assertEqual(low_collateral["reason"], "insufficient_free_collateral")
        self.assertEqual(daily["reason"], "daily_loss_limit_reached")
        self.assertEqual(drawdown["reason"], "portfolio_drawdown_limit_reached")

    def test_rejects_unknown_or_nonfinite_data_without_raising(self):
        result = evaluate_perps_entry(
            snapshot(mark_price="NaN"), side="LONG", requested_notional="10",
            equity="100", free_collateral="100",
        )
        self.assertEqual(result["reason"], "invalid_or_incomplete_risk_input")


if __name__ == "__main__":
    unittest.main()
