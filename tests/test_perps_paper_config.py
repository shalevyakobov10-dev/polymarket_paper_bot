import unittest

from perps_paper_config import perps_engine_options_from_env


class PerpsPaperConfigTests(unittest.TestCase):
    def test_defaults_match_documented_isolated_margin_limits(self):
        options = perps_engine_options_from_env({})
        self.assertEqual(options["initial_equity"], "1000")
        self.assertEqual(options["max_position_notional"], "25")
        self.assertEqual(options["max_total_notional"], "75")
        self.assertEqual(options["max_open_positions"], 3)
        self.assertEqual(options["max_leverage"], "2")
        self.assertEqual(options["risk_per_trade_pct"], "0.0025")
        self.assertEqual(options["stop_loss_pct"], "0.0075")
        self.assertEqual(options["take_profit_pct"], "0.015")
        self.assertEqual(options["funding_schedule_grace_seconds"], 60)
        self.assertEqual(options["max_post_funding_mark_delay_seconds"], 90)

    def test_overrides_are_shared_as_normalized_engine_options(self):
        options = perps_engine_options_from_env({
            "PERPS_PAPER_INITIAL_EQUITY": "2500",
            "PERPS_PAPER_MAX_POSITION_NOTIONAL": "40",
            "PERPS_PAPER_MAX_TOTAL_NOTIONAL": "120",
            "PERPS_PAPER_MAX_OPEN_POSITIONS": "4",
            "PERPS_PAPER_MAX_LEVERAGE": "1.5",
            "PERPS_PAPER_LOOKBACK_OBSERVATIONS": "12",
            "PERPS_PAPER_MIN_HISTORY_SPAN_SECONDS": "360",
            "PERPS_PAPER_MAX_ENTRY_IMPACT_BPS": "35",
            "PERPS_PAPER_FUNDING_SCHEDULE_GRACE_SECONDS": "90",
            "PERPS_PAPER_MAX_POST_FUNDING_MARK_DELAY_SECONDS": "60",
        })
        self.assertEqual(options["initial_equity"], "2500")
        self.assertEqual(options["max_position_notional"], "40")
        self.assertEqual(options["max_total_notional"], "120")
        self.assertEqual(options["max_open_positions"], 4)
        self.assertEqual(options["max_leverage"], "1.5")
        self.assertEqual(options["lookback_observations"], 12)
        self.assertEqual(options["minimum_history_span_seconds"], 360)
        self.assertEqual(options["max_entry_impact_bps"], "35")
        self.assertEqual(options["funding_schedule_grace_seconds"], 90)
        self.assertEqual(options["max_post_funding_mark_delay_seconds"], 60)

    def test_invalid_integer_settings_fail_closed(self):
        cases = (
            ({"PERPS_PAPER_MAX_OPEN_POSITIONS": "0"}, "MAX_OPEN_POSITIONS"),
            ({"PERPS_PAPER_LOOKBACK_OBSERVATIONS": "2"}, "LOOKBACK_OBSERVATIONS"),
            ({"PERPS_PAPER_MIN_HISTORY_SPAN_SECONDS": "-1"}, "MIN_HISTORY_SPAN_SECONDS"),
            ({"PERPS_PAPER_FUNDING_SCHEDULE_GRACE_SECONDS": "4"}, "FUNDING_SCHEDULE_GRACE_SECONDS"),
            ({"PERPS_PAPER_MAX_POST_FUNDING_MARK_DELAY_SECONDS": "121"}, "MAX_POST_FUNDING_MARK_DELAY_SECONDS"),
        )
        for env, expected in cases:
            with self.subTest(env=env):
                with self.assertRaisesRegex(ValueError, expected):
                    perps_engine_options_from_env(env)


if __name__ == "__main__":
    unittest.main()
