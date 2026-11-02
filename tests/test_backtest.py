import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from backtest import diagnose_price_changes, evaluate_forward_returns, fetch_price_history, list_field, run_backtest, run_threshold_sensitivity, run_execution_cost_sensitivity, run_walk_forward_backtest, run_segment
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

    def test_forward_returns_compares_signals_with_same_window_baseline(self):
        # Signal: price increases from 0.50 to 0.56; one-hour later it falls.
        prices = [0.50, 0.56, 0.50, 0.52, 0.53, 0.54, 0.55]
        history = [
            {"timestamp": 1_800_000_000 + i * 3600,
             "token_id": "token-a", "observed_price": price}
            for i, price in enumerate(prices)
        ]
        result = evaluate_forward_returns(history, threshold=0.03, horizons_hours=(1,))
        stats = result["horizons"]["1h"]
        self.assertEqual(stats["signals_detected"], 1)
        self.assertEqual(stats["signal_forward_returns"]["n"], 1)
        self.assertLess(stats["signal_forward_returns"]["mean_return_pct"], 0)
        self.assertGreater(stats["all_observation_baseline"]["n"], 0)
        self.assertIsNotNone(stats["signal_minus_baseline_mean_return_pct"])

    def test_forward_returns_uses_prior_price_for_first_holdout_observation(self):
        history = [
            {"timestamp": 1_800_000_000, "token_id": "token-a", "observed_price": 0.56},
            {"timestamp": 1_800_003_600, "token_id": "token-a", "observed_price": 0.50},
        ]
        result = evaluate_forward_returns(
            history, threshold=0.03, horizons_hours=(1,), prior_prices={"token-a": 0.50}
        )
        self.assertEqual(result["horizons"]["1h"]["signals_detected"], 1)
        self.assertEqual(result["horizons"]["1h"]["signal_forward_returns"]["n"], 1)

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



    def test_standard_backtest_ignores_malformed_history_rows(self):
        history = [
            {"timestamp": 1_800_000_000 + index * 3600, "token_id": "t",
             "observed_price": 0.5 + (0.01 if index % 2 else 0.0)}
            for index in range(12)
        ]
        history.extend([
            {"timestamp": 1_800_000_000.5, "token_id": "t", "observed_price": 0.6},
            {"timestamp": float("nan"), "token_id": "t", "observed_price": 0.6},
            {"timestamp": 1_800_000_000, "token_id": "t", "observed_price": float("inf")},
            None,
        ])
        report = run_backtest(history, PaperConfig(), train_ratio=0.7)
        self.assertEqual(report["data"]["unique_timestamps"], 12)
        self.assertEqual(report["data"]["price_observations"], 12)


    def test_execution_cost_sensitivity_replays_fixed_strategy_across_cost_grid(self):
        prices = [0.40, 0.44, 0.50, 0.48, 0.52, 0.46, 0.50, 0.47, 0.51, 0.48, 0.53, 0.50]
        history = [
            {
                "timestamp": 1_800_000_000 + i * 3600,
                "token_id": "token-a",
                "market_id": "market-a",
                "question": "Synthetic cost test",
                "outcome": "Yes",
                "observed_price": price,
            }
            for i, price in enumerate(prices)
        ]
        scenarios = run_execution_cost_sensitivity(
            history,
            PaperConfig(),
            train_ratio=0.7,
            fee_rates=(0.0, 0.01),
            slippage_rates=(0.005, 0.02),
        )
        self.assertEqual(len(scenarios), 4)
        self.assertEqual(
            [(row["fee_rate_assumption_each_side"], row["slippage_rate_each_side"]) for row in scenarios],
            [(0.0, 0.005), (0.0, 0.02), (0.01, 0.005), (0.01, 0.02)],
        )
        self.assertTrue(all(row["same_strategy_and_history"] for row in scenarios))
        self.assertTrue(all(row["execution"] == "none" for row in scenarios))
        self.assertTrue(all("return_pct" in row["out_of_sample"] for row in scenarios))

    def test_execution_cost_sensitivity_rejects_invalid_cost_assumptions(self):
        with self.assertRaises(ValueError):
            run_execution_cost_sensitivity([], PaperConfig(), fee_rates=())
        with self.assertRaises(ValueError):
            run_execution_cost_sensitivity([], PaperConfig(), fee_rates=(-0.01,))
        with self.assertRaises(ValueError):
            run_execution_cost_sensitivity([], PaperConfig(), slippage_rates=(float("nan"),))


    def test_walk_forward_uses_expanding_train_and_non_overlapping_test_windows(self):
        prices = [0.40, 0.46, 0.43, 0.51, 0.48, 0.55, 0.50, 0.58, 0.54, 0.61,
                  0.56, 0.64, 0.59, 0.67, 0.62, 0.70, 0.65, 0.73, 0.68, 0.76,
                  0.71, 0.79, 0.74, 0.82, 0.77, 0.85, 0.80, 0.88, 0.83, 0.90]
        history = [
            {"timestamp": 1_800_000_000 + index * 3600, "token_id": "token-a",
             "market_id": "market-a", "observed_price": price}
            for index, price in enumerate(prices)
        ]
        report = run_walk_forward_backtest(
            history, PaperConfig(), folds=3, initial_train_ratio=0.5
        )
        self.assertEqual(report["mode"], "EXPANDING_WALK_FORWARD_PAPER_BACKTEST")
        self.assertTrue(report["strategy_fixed_across_folds"])
        self.assertTrue(report["test_windows_non_overlapping"])
        self.assertTrue(report["portfolio_reset_each_fold"])
        self.assertEqual(len(report["folds"]), 3)
        for index, fold in enumerate(report["folds"]):
            self.assertLess(
                fold["training_window"]["end_utc"],
                fold["test_window"]["start_utc"],
            )
            self.assertEqual(fold["research_only"], True)
            self.assertEqual(fold["execution"], "none")
            if index:
                self.assertLess(
                    report["folds"][index - 1]["test_window"]["end_utc"],
                    fold["test_window"]["start_utc"],
                )
                self.assertGreater(
                    fold["training_window"]["unique_timestamps"],
                    report["folds"][index - 1]["training_window"]["unique_timestamps"],
                )
        self.assertEqual(report["aggregate_out_of_sample"]["total_test_observations"], 15)
        self.assertIn("weighted_win_rate_pct", report["aggregate_out_of_sample"])
        self.assertIn("worst_fold_max_drawdown_pct", report["aggregate_out_of_sample"])
        self.assertTrue(all("max_drawdown_pct" in fold["out_of_sample"] for fold in report["folds"]))

    def test_walk_forward_requires_two_timestamps_in_every_test_fold(self):
        history = [
            {"timestamp": 1_800_000_000 + index * 3600, "token_id": "t",
             "observed_price": 0.5 + index * 0.001}
            for index in range(9)
        ]
        with self.assertRaisesRegex(ValueError, "at least 2 unique timestamps"):
            run_walk_forward_backtest(
                history, PaperConfig(), folds=3, initial_train_ratio=0.5
            )

    def test_history_normalization_rejects_fractional_and_nonfinite_timestamps(self):
        history = [
            {"timestamp": 1_800_000_000 + index * 3600, "token_id": "t",
             "observed_price": 0.5 + index * 0.001}
            for index in range(16)
        ]
        history.extend([
            {"timestamp": 1_800_000_000.5, "token_id": "t", "observed_price": 0.6},
            {"timestamp": float("nan"), "token_id": "t", "observed_price": 0.6},
            {"timestamp": 1_800_000_000, "token_id": "t", "observed_price": float("inf")},
            None,
        ])
        report = run_walk_forward_backtest(
            history, PaperConfig(), folds=3, initial_train_ratio=0.5
        )
        self.assertEqual(report["data"]["unique_timestamps"], 16)
        self.assertEqual(report["data"]["price_observations"], 16)

    def test_segment_reports_snapshot_marked_maximum_drawdown(self):
        prices = [0.50, 0.54, 0.50, 0.45, 0.47, 0.50]
        history = [
            {"timestamp": 1_800_000_000 + index * 3600, "token_id": "drawdown-token",
             "market_id": "drawdown-market", "question": "Drawdown test",
             "outcome": "Yes", "observed_price": price}
            for index, price in enumerate(prices)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            report = run_segment(history, PaperConfig(), Path(temp_dir) / "state.json")
        self.assertGreater(report["max_drawdown_usd"], 0.0)
        self.assertGreater(report["max_drawdown_pct"], 0.0)
        self.assertIn("intraperiod", report["drawdown_method"])

    def test_walk_forward_rejects_invalid_fold_configuration_and_insufficient_history(self):
        history = [
            {"timestamp": 1_800_000_000 + index * 3600, "token_id": "t",
             "observed_price": 0.5 + index * 0.001}
            for index in range(12)
        ]
        with self.assertRaises(ValueError):
            run_walk_forward_backtest(history, PaperConfig(), folds=1)
        with self.assertRaises(ValueError):
            run_walk_forward_backtest(history, PaperConfig(), folds=3, initial_train_ratio=0.95)
        with self.assertRaises(ValueError):
            run_walk_forward_backtest(history[:6], PaperConfig(), folds=2)
        with self.assertRaises(ValueError):
            run_walk_forward_backtest(history[:8], PaperConfig(), folds=5, initial_train_ratio=0.5)

if __name__ == "__main__":
    unittest.main()
