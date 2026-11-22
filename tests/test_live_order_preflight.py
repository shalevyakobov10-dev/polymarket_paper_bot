import unittest

from live_order_preflight import (
    LIVE_ORDER_SUBMISSION_IMPLEMENTED,
    REQUIRED_RELEASE_GATES,
    evaluate_live_order_preflight,
)


def valid_args():
    return {
        "token_id": "token-123",
        "side": "BUY",
        "limit_price": 0.50,
        "size": 10,
        "best_ask": 0.50,
        "tick_size": 0.01,
        "minimum_order_size": 5,
        "book_age_seconds": 2,
        "max_book_age_seconds": 10,
        "max_price_impact_bps": 100,
        "max_order_notional_usd": 20,
        "available_collateral_usd": 100,
        "estimated_fees_usd": 0.10,
        "current_open_positions": 0,
        "max_open_positions": 5,
        "opposing_market_outcome_open": False,
        "current_open_exposure_usd": 0,
        "max_open_exposure_usd": 100,
        "daily_realized_pnl_usd": 0,
        "open_unrealized_pnl_usd": 0,
        "daily_loss_limit_usd": 25,
        "current_drawdown_pct": 0,
        "max_drawdown_pct": 10,
        "market_active": True,
        "kill_switch_engaged": False,
        "release_gates": {gate: True for gate in REQUIRED_RELEASE_GATES},
    }


class LiveOrderPreflightTests(unittest.TestCase):
    def test_valid_inputs_only_pass_preflight_not_submission(self):
        result = evaluate_live_order_preflight(**valid_args())
        self.assertTrue(result["preflight_passed"])
        self.assertEqual(result["status"], "checks_passed_not_authorized")
        self.assertFalse(result["order_submission_implemented"])
        self.assertFalse(result["order_submission_allowed"])
        self.assertFalse(result["order_signing_allowed"])
        self.assertEqual(result["execution"], "none")
        self.assertEqual(result["order_notional_usd"], 5.0)
        self.assertFalse(LIVE_ORDER_SUBMISSION_IMPLEMENTED)


    def test_insufficient_collateral_after_fees_blocks_entry(self):
        args = valid_args()
        args["available_collateral_usd"] = 5.05
        args["estimated_fees_usd"] = 0.10
        result = evaluate_live_order_preflight(**args)
        self.assertIn("insufficient_available_collateral_after_fees", result["blockers"])

    def test_open_position_cap_and_opposing_outcome_are_independent_blocks(self):
        args = valid_args()
        args["current_open_positions"] = 5
        args["max_open_positions"] = 5
        args["opposing_market_outcome_open"] = True
        result = evaluate_live_order_preflight(**args)
        self.assertIn("max_open_positions_reached", result["blockers"])
        self.assertIn("opposing_market_outcome_already_open", result["blockers"])

    def test_invalid_collateral_fee_and_position_inputs_fail_closed(self):
        args = valid_args()
        args["available_collateral_usd"] = -1
        args["estimated_fees_usd"] = -0.01
        args["current_open_positions"] = True
        result = evaluate_live_order_preflight(**args)
        self.assertIn("invalid_available_collateral", result["blockers"])
        self.assertIn("invalid_estimated_fees", result["blockers"])
        self.assertIn("invalid_open_position_count", result["blockers"])

    def test_missing_release_gate_fails_closed(self):
        args = valid_args()
        args["release_gates"]["jurisdiction_and_legal_review_approved"] = False
        result = evaluate_live_order_preflight(**args)
        self.assertFalse(result["preflight_passed"])
        self.assertIn(
            "release_gate_not_verified:jurisdiction_and_legal_review_approved",
            result["blockers"],
        )

    def test_kill_switch_and_inactive_market_block(self):
        args = valid_args()
        args["kill_switch_engaged"] = True
        args["market_active"] = False
        result = evaluate_live_order_preflight(**args)
        self.assertIn("kill_switch_engaged", result["blockers"])
        self.assertIn("market_not_confirmed_active", result["blockers"])

    def test_stale_book_and_excessive_price_impact_block(self):
        args = valid_args()
        args["book_age_seconds"] = 11
        args["limit_price"] = 0.52
        result = evaluate_live_order_preflight(**args)
        self.assertIn("stale_or_invalid_order_book", result["blockers"])
        self.assertIn("price_impact_limit_exceeded", result["blockers"])

    def test_tick_minimum_size_and_notional_are_enforced(self):
        args = valid_args()
        args["limit_price"] = 0.505
        args["size"] = 2
        args["max_order_notional_usd"] = 0.5
        result = evaluate_live_order_preflight(**args)
        self.assertIn("limit_price_not_on_tick", result["blockers"])
        self.assertIn("below_minimum_order_size", result["blockers"])
        self.assertIn("max_order_notional_exceeded", result["blockers"])

    def test_daily_loss_drawdown_and_exposure_limits_block(self):
        args = valid_args()
        args["daily_realized_pnl_usd"] = -30
        args["current_drawdown_pct"] = 10
        args["current_open_exposure_usd"] = 98
        result = evaluate_live_order_preflight(**args)
        self.assertIn("daily_loss_limit_reached", result["blockers"])
        self.assertIn("max_drawdown_reached", result["blockers"])
        self.assertIn("max_open_exposure_exceeded", result["blockers"])

    def test_invalid_numeric_values_fail_closed(self):
        args = valid_args()
        args["limit_price"] = float("nan")
        args["size"] = float("inf")
        result = evaluate_live_order_preflight(**args)
        self.assertFalse(result["preflight_passed"])
        self.assertIn("invalid_limit_price", result["blockers"])
        self.assertIn("invalid_size", result["blockers"])


if __name__ == "__main__":
    unittest.main()
