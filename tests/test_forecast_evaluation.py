import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from forecast_evaluation import (
    build_binary_forecast_samples,
    chronological_holdout_evaluation,
    evaluate_jsonl_files,
    normalize_binary_resolution,
    score_binary_forecasts,
)


class ForecastEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.resolved_at = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)

    def market(self, yes_wins=True):
        prices = ["1", "0"] if yes_wins else ["0", "1"]
        return {
            "id": "123",
            "conditionId": "condition-123",
            "slug": "will-it-happen",
            "question": "Will it happen?",
            "closed": True,
            "closedTime": self.resolved_at.isoformat(),
            "outcomes": '["Yes", "No"]',
            "outcomePrices": json.dumps(prices),
            "clobTokenIds": '["yes-token", "no-token"]',
            "resolutionSource": "https://example.org",
        }

    def snapshot(self, when, price, market_id="condition-123", token_id="yes-token"):
        return {
            "market_id": market_id,
            "token_id": token_id,
            "outcome": "Yes",
            "timestamp": when.isoformat(),
            "observed_price": price,
            "question": "Will it happen?",
        }

    def test_normalizes_decisive_yes_resolution(self):
        result = normalize_binary_resolution(self.market(True))
        self.assertIsNotNone(result)
        self.assertEqual(result["market_id"], "condition-123")
        self.assertEqual(result["yes_token_id"], "yes-token")
        self.assertEqual(result["resolved_yes"], 1)
        self.assertEqual(result["source"], "public_gamma_resolved_market")

    def test_normalizes_decisive_no_resolution(self):
        result = normalize_binary_resolution(self.market(False))
        self.assertEqual(result["resolved_yes"], 0)

    def test_rejects_open_nonbinary_and_unresolved_markets(self):
        open_market = {**self.market(), "closed": False}
        self.assertIsNone(normalize_binary_resolution(open_market))
        self.assertIsNone(normalize_binary_resolution({**self.market(), "outcomes": '["A","B","C"]'}))
        self.assertIsNone(normalize_binary_resolution({**self.market(), "outcomePrices": '["0.6","0.4"]'}))

    def test_builds_one_sample_near_24h_before_resolution_without_future_leakage(self):
        target = self.resolved_at - timedelta(hours=24)
        snapshots = [
            self.snapshot(target - timedelta(hours=2), 0.4),
            self.snapshot(target - timedelta(minutes=20), 0.7),
            self.snapshot(target + timedelta(minutes=10), 0.99),
            self.snapshot(self.resolved_at + timedelta(minutes=5), 1.0),
        ]
        resolution = normalize_binary_resolution(self.market(True))
        samples = build_binary_forecast_samples(snapshots, [resolution])
        self.assertEqual(len(samples), 1)
        self.assertAlmostEqual(samples[0]["predicted_probability_yes"], 0.99)
        self.assertLess(samples[0]["horizon_hours_actual"], 24.0)
        self.assertLess(samples[0]["horizon_hours_actual"], 24.5)

    def test_excludes_snapshot_outside_horizon_tolerance_and_wrong_market(self):
        too_old = self.snapshot(self.resolved_at - timedelta(hours=50), 0.5)
        wrong_market = self.snapshot(self.resolved_at - timedelta(hours=24), 0.6, market_id="other")
        samples = build_binary_forecast_samples(
            [too_old, wrong_market], [normalize_binary_resolution(self.market())],
        )
        self.assertEqual(samples, [])

    def test_rejects_invalid_horizon_settings(self):
        with self.assertRaises(ValueError):
            build_binary_forecast_samples([], [], horizon_hours=0)
        with self.assertRaises(ValueError):
            build_binary_forecast_samples([], [], tolerance_hours=-1)

    def test_perfect_forecasts_have_low_brier_and_log_loss(self):
        metrics = score_binary_forecasts([
            {"predicted_probability_yes": 0.99, "outcome_yes": 1},
            {"predicted_probability_yes": 0.01, "outcome_yes": 0},
        ])
        self.assertEqual(metrics["n"], 2)
        self.assertLess(metrics["brier_score"], 0.001)
        self.assertLess(metrics["log_loss"], 0.02)
        self.assertEqual(metrics["accuracy_pct"], 100.0)

    def test_baseline_skill_and_calibration_are_reported(self):
        metrics = score_binary_forecasts([
            {"predicted_probability_yes": 0.8, "outcome_yes": 1},
            {"predicted_probability_yes": 0.2, "outcome_yes": 0},
            {"predicted_probability_yes": 0.7, "outcome_yes": 1},
            {"predicted_probability_yes": 0.3, "outcome_yes": 0},
        ])
        self.assertGreater(metrics["brier_skill_vs_sample_base_rate"], 0)
        self.assertIsNotNone(metrics["expected_calibration_error"])
        self.assertTrue(metrics["calibration_bins"])

    def test_chronological_holdout_uses_earlier_base_rate_on_later_markets(self):
        samples = []
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        outcomes = [1, 1, 0, 0, 1, 0, 1, 0, 1, 0] * 4
        for index, outcome in enumerate(outcomes):
            samples.append({
                "resolved_at": (start + timedelta(days=index)).isoformat(),
                "predicted_probability_yes": 0.8 if outcome else 0.2,
                "outcome_yes": outcome,
            })
        result = chronological_holdout_evaluation(
            samples, train_fraction=0.7, minimum_train=20, minimum_test=10,
        )
        self.assertEqual(result["status"], "scored")
        self.assertEqual(result["train_n"], 28)
        self.assertEqual(result["test_n"], 12)
        self.assertIsNotNone(result["test_brier_score"])
        self.assertIsNotNone(result["test_brier_skill_vs_train_base_rate"])

    def test_chronological_holdout_refuses_small_samples(self):
        samples = [{
            "resolved_at": "2026-01-01T00:00:00Z",
            "predicted_probability_yes": 0.6,
            "outcome_yes": 1,
        }] * 5
        result = chronological_holdout_evaluation(samples)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIsNone(result["test_brier_score"])

    def test_chronological_holdout_rejects_invalid_configuration(self):
        with self.assertRaises(ValueError):
            chronological_holdout_evaluation([], train_fraction=0.4)
        with self.assertRaises(ValueError):
            chronological_holdout_evaluation([], minimum_train=0)

    def test_empty_or_invalid_samples_return_explicit_warning(self):
        metrics = score_binary_forecasts([
            {"predicted_probability_yes": float("nan"), "outcome_yes": 1},
            {"predicted_probability_yes": 0.5, "outcome_yes": "unknown"},
        ])
        self.assertEqual(metrics["n"], 0)
        self.assertIsNone(metrics["brier_score"])
        self.assertIn("No eligible", metrics["warning"])

    def test_jsonl_file_evaluation_is_resilient_to_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshots = root / "snapshots.jsonl"
            resolutions = root / "resolutions.jsonl"
            target = self.resolved_at - timedelta(hours=24)
            snapshots.write_text(
                json.dumps(self.snapshot(target, 0.8)) + "\n{bad json\n",
                encoding="utf-8",
            )
            resolutions.write_text(
                json.dumps(normalize_binary_resolution(self.market(True))) + "\n",
                encoding="utf-8",
            )
            report = evaluate_jsonl_files([snapshots], [resolutions])
            self.assertEqual(report["eligible_samples"], 1)
            self.assertEqual(report["metrics"]["n"], 1)
            self.assertEqual(report["mode"], "RESOLVED_MARKET_FORECAST_EVALUATION")


if __name__ == "__main__":
    unittest.main()
