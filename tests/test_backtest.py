import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from backtest import diagnose_price_changes, fetch_price_history, list_field, run_backtest, run_threshold_sensitivity
from paper_trading import PaperConfig


class BacktestTests(unittest.TestCase):
    def test_list_field_accepts_json_and_rejects_bad_values(self):
        self.assertEqual(list_field('["a", "b"]'), ["a", "b"])
        self.assertEqual(list_field({"not": "a list"}), [])
        self.assertEqual(list_field("invalid"), [])

    def test_backtest_uses_chronological_train_test_split(self):
        prices = [0.40, 0.44, 0.50, 0.48, 0.52, 0.46, 0.50, 0.47, 0.51, 0.48, 0.53, 0.50]
        history = [
            {
                "timestamp": 1_800_000_000 + i * 3600,
                "token_id": "token-a",
                "market_id": "market-a",
                "question": "Synthetic test market",
                "outcome": "Yes",
                "observed_price": price,
            }
            for i, price in enumerate(prices)
        ]
        report = run_backtest(history, PaperConfig(), train_ratio=0.7)
        self.assertEqual(report["data"]["split_method"], "chronological")
        self.assertLess(
            report["in_sample"]["window_end_utc"],
            report["out_of_sample"]["window_start_utc"],
        )
        self.assertEqual(report["in_sample"]["metrics"]["cash"] >= 0, True)
        self.assertEqual(report["out_of_sample"]["metrics"]["cash"] >= 0, True)
        self.assertEqual(report["out_of_sample"]["open_positions_at_end"] <= 5, True)
        self.assertIn("does not guarantee future performance", " ".join(report["limitations"]).lower())

    def test_fetch_price_history_uses_interval_and_filters_requested_window(self):
        token = {"token_id": "token-123", "market_id": "m", "question": "q", "outcome": "Yes"}
        payload = {
            "history": [
                {"t": 99, "p": "0.4"},
                {"t": 100, "p": "0.41"},
                {"t": 150, "p": "0.5"},
                {"t": 200, "p": "0.6"},
                {"t": 201, "p": "0.7"},
            ]
        }
        with patch("backtest.get_json", return_value=payload) as mocked:
            rows = fetch_price_history(token, start_ts=100, end_ts=200, fidelity_minutes=60)
        self.assertEqual([row["timestamp"] for row in rows], [100, 150, 200])
        query = parse_qs(urlparse(mocked.call_args.args[0]).query)
        self.assertEqual(query["market"], ["token-123"])
        self.assertEqual(query["interval"], ["max"])
        self.assertEqual(query["fidelity"], ["60"])
        self.assertNotIn("startTs", query)
        self.assertNotIn("endTs", query)

    def test_price_change_diagnostics_count_threshold_crossings(self):
        history = [
            {"timestamp": 1, "token_id": "a", "observed_price": 0.50},
            {"timestamp": 2, "token_id": "a", "observed_price": 0.51},
            {"timestamp": 3, "token_id": "a", "observed_price": 0.55},
            {"timestamp": 4, "token_id": "a", "observed_price": 0.53},
        ]
        result = diagnose_price_changes(history)
        self.assertEqual(result["consecutive_price_pairs"], 3)
        self.assertEqual(result["positive_moves_by_minimum_delta"]["0.0050"], 2)
        self.assertEqual(result["positive_moves_by_minimum_delta"]["0.0300"], 1)
        self.assertAlmostEqual(result["largest_positive_delta"], 0.04)
        self.assertAlmostEqual(result["largest_negative_delta_abs"], 0.02)

    def test_threshold_sensitivity_reuses_same_history_for_each_preset(self):
        prices = [0.40, 0.41, 0.45, 0.43, 0.48, 0.46, 0.50, 0.47, 0.52, 0.50, 0.54, 0.51]
        history = [
            {"timestamp": 1_800_000_000 + i * 3600, "token_id": "token-a",
             "observed_price": price}
            for i, price in enumerate(prices)
        ]
        results = run_threshold_sensitivity(history, PaperConfig(), train_ratio=0.7, thresholds=(0.005, 0.03))
        self.assertEqual([row["signal_delta"] for row in results], [0.005, 0.03])
        self.assertIn("return_pct", results[0]["in_sample"])
        self.assertIn("return_pct", results[1]["out_of_sample"])

    def test_backtest_rejects_too_few_points(self):
        history = [
            {"timestamp": 1_800_000_000 + i, "token_id": "t", "observed_price": 0.5}
            for i in range(5)
        ]
        with self.assertRaises(ValueError):
            run_backtest(history, PaperConfig())

    def test_backtest_requires_valid_chronological_ratio(self):
        with self.assertRaises(ValueError):
            run_backtest([], PaperConfig(), train_ratio=0.2)


if __name__ == "__main__":
    unittest.main()
