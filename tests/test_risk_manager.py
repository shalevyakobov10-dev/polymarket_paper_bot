import unittest

from risk_manager import evaluate_paper_entry


class PaperRiskManagerTests(unittest.TestCase):
    def evaluate(self, **overrides):
        values = {
            "price": 0.5,
            "requested_notional": 2.0,
            "cash": 100.0,
            "open_exposure": 0.0,
            "open_positions": 0,
            "max_position_usd": 2.0,
            "max_open_exposure_usd": 10.0,
            "max_open_positions": 5,
            "daily_realized_pnl": 0.0,
            "open_unrealized_pnl": 0.0,
            "daily_loss_limit_usd": 5.0,
            "fee_rate": 0.0,
        }
        values.update(overrides)
        return evaluate_paper_entry(**values)

    def test_approves_valid_paper_entry(self):
        decision = self.evaluate()
        self.assertTrue(decision.approved)
        self.assertEqual(decision.reason, "approved_paper_entry")
        self.assertEqual(decision.approved_notional, 2.0)

    def test_daily_stop_includes_unrealized_losses(self):
        decision = self.evaluate(open_unrealized_pnl=-5.0)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "daily_loss_limit_reached")

    def test_daily_stop_combines_realized_and_unrealized_losses(self):
        decision = self.evaluate(daily_realized_pnl=-3.0, open_unrealized_pnl=-2.1)
        self.assertEqual(decision.reason, "daily_loss_limit_reached")

    def test_position_and_aggregate_exposure_limits(self):
        self.assertEqual(self.evaluate(open_positions=5).reason, "max_open_positions_reached")
        self.assertEqual(self.evaluate(open_exposure=10.0).reason, "max_open_exposure_reached")
        self.assertEqual(self.evaluate(open_exposure=9.0).reason, "max_open_exposure_reached")
        self.assertEqual(self.evaluate(requested_notional=2.1).reason, "max_position_size_reached")

    def test_cash_includes_simulated_entry_fee(self):
        decision = self.evaluate(cash=2.0, fee_rate=0.01)
        self.assertEqual(decision.reason, "insufficient_simulated_cash_after_fee")

    def test_rejects_opposing_outcome_when_same_market_is_already_open(self):
        decision = self.evaluate(opposing_market_outcome_open=True)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "opposing_outcome_already_open")

    def test_rejects_invalid_prices_and_nonfinite_inputs(self):
        self.assertEqual(self.evaluate(price=0.0).reason, "price_outside_entry_range")
        self.assertEqual(self.evaluate(price=1.0).reason, "price_outside_entry_range")
        self.assertEqual(self.evaluate(cash=float("nan")).reason, "invalid_risk_input")


if __name__ == "__main__":
    unittest.main()
