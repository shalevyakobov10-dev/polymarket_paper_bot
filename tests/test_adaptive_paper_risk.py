import unittest

from adaptive_paper_risk import (
    daily_drawdown_breached,
    dynamic_exit_levels,
    size_position_by_risk,
)


class AdaptivePaperRiskTests(unittest.TestCase):
    def test_dynamic_levels_are_bounded_and_reward_to_risk_is_applied(self):
        levels = dynamic_exit_levels(
            entry_price=0.50,
            price_history=[0.48, 0.49, 0.50, 0.51, 0.50, 0.52],
            volatility_multiplier=2,
            reward_to_risk=2,
            minimum_stop_distance=0.02,
            maximum_stop_distance=0.15,
        )
        self.assertLess(levels.stop_loss_price, 0.50)
        self.assertGreater(levels.take_profit_price, 0.50)
        self.assertLessEqual(levels.stop_distance, 0.15)
        self.assertAlmostEqual(levels.take_distance, levels.stop_distance * 2, places=6)

    def test_missing_history_uses_fallback_volatility(self):
        levels = dynamic_exit_levels(
            entry_price=0.50, price_history=[], fallback_volatility=0.02,
            volatility_multiplier=2, minimum_stop_distance=0.01,
            maximum_stop_distance=0.15,
        )
        self.assertAlmostEqual(levels.stop_distance, 0.04, places=6)

    def test_risk_sizing_never_exceeds_risk_budget(self):
        result = size_position_by_risk(
            equity=100, available_cash=100, remaining_exposure_capacity=100,
            entry_price=0.50, stop_distance=0.05, risk_per_trade_pct=0.01,
            max_position_usd=100,
        )
        self.assertTrue(result["approved"])
        self.assertLessEqual(result["estimated_loss_at_stop_usd"], 1.000001)
        self.assertLessEqual(result["notional_usd"], 10.000001)

    def test_position_sizing_is_capped_by_cash_and_exposure(self):
        result = size_position_by_risk(
            equity=100, available_cash=2, remaining_exposure_capacity=1,
            entry_price=0.50, stop_distance=0.05, risk_per_trade_pct=0.01,
            max_position_usd=20,
        )
        self.assertEqual(result["notional_usd"], 1.0)

    def test_daily_drawdown_is_hard_gate(self):
        self.assertTrue(daily_drawdown_breached(
            day_start_equity=100, current_equity=94, max_daily_drawdown_pct=0.05
        ))
        self.assertFalse(daily_drawdown_breached(
            day_start_equity=100, current_equity=96, max_daily_drawdown_pct=0.05
        ))


if __name__ == "__main__":
    unittest.main()
