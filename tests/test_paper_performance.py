import unittest

from paper_performance import evaluate_paper_performance


def trade(trade_id, pnl, *, entry_fee=0.1, exit_fee=0.1, opened="2026-11-01T10:00:00+00:00", closed="2026-11-01T11:00:00+00:00"):
    return {
        "trade_id": trade_id,
        "pnl": pnl,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "opened_at": opened,
        "closed_at": closed,
    }


class PaperPerformanceTests(unittest.TestCase):
    def test_empty_sample_is_explicitly_insufficient(self):
        report = evaluate_paper_performance([], starting_cash=100)
        self.assertEqual(report["evaluation_status"], "insufficient_sample")
        self.assertEqual(report["closed_trades"], 0)
        self.assertIsNone(report["win_rate_pct"])
        self.assertIsNone(report["net_expectancy_per_trade"])
        self.assertFalse(report["out_of_sample_validated"])
        self.assertFalse(report["live_execution_eligible"])

    def test_net_statistics_include_fees_and_wilson_interval(self):
        rows = [trade(i, 2.0) for i in range(40)] + [trade(i + 40, -1.0) for i in range(20)]
        report = evaluate_paper_performance(rows, starting_cash=100)
        self.assertEqual(report["closed_trades"], 60)
        self.assertEqual(report["wins"], 40)
        self.assertEqual(report["losses"], 20)
        self.assertEqual(report["win_rate_pct"], 100 * 40 / 60)
        self.assertEqual(report["total_net_pnl"], 60.0)
        self.assertAlmostEqual(report["fees_paid"], 12.0)
        self.assertAlmostEqual(report["gross_pnl_before_fees"], 72.0)
        self.assertAlmostEqual(report["net_expectancy_per_trade"], 1.0)
        self.assertAlmostEqual(report["net_profit_factor"], 4.0)
        self.assertIsNotNone(report["win_rate_95pct_wilson_interval_pct"])
        self.assertEqual(report["evaluation_status"], "positive_in_sample_unvalidated")

    def test_drawdown_follows_chronological_close_order(self):
        rows = [
            trade(1, 10, closed="2026-11-01T10:00:00+00:00"),
            trade(2, -15, closed="2026-11-01T11:00:00+00:00"),
            trade(3, 5, closed="2026-11-01T12:00:00+00:00"),
        ]
        report = evaluate_paper_performance(rows, starting_cash=100)
        self.assertAlmostEqual(report["max_closed_trade_drawdown"], 15.0)
        self.assertAlmostEqual(report["max_closed_trade_drawdown_pct"], 13.6363636364, places=6)

    def test_open_and_invalid_timestamps_do_not_invent_holding_time(self):
        report = evaluate_paper_performance(
            [trade(1, 1, opened="not-a-time", closed="also-not-a-time")],
            starting_cash=100,
            minimum_trades=1,
        )
        self.assertIsNone(report["average_hold_hours"])
        self.assertEqual(report["evaluation_status"], "positive_in_sample_unvalidated")

    def test_invalid_rows_are_excluded_and_reported(self):
        report = evaluate_paper_performance(
            [trade(1, 1), {"pnl": float("nan")}, None],
            starting_cash=100,
            minimum_trades=1,
        )
        self.assertEqual(report["closed_trades"], 1)
        self.assertEqual(report["invalid_rows_excluded"], 2)
        self.assertTrue(any("invalid trade rows" in item for item in report["warnings"]))

    def test_no_losses_does_not_report_infinite_profit_factor(self):
        report = evaluate_paper_performance([trade(1, 2)], starting_cash=100, minimum_trades=1)
        self.assertIsNone(report["net_profit_factor"])

    def test_rejects_bad_starting_cash_or_sample_threshold(self):
        for bad in (0, -1, float("nan"), float("inf")):
            with self.subTest(starting_cash=bad):
                with self.assertRaises(ValueError):
                    evaluate_paper_performance([], starting_cash=bad)
        with self.assertRaises(ValueError):
            evaluate_paper_performance([], starting_cash=100, minimum_trades=0)


if __name__ == "__main__":
    unittest.main()
