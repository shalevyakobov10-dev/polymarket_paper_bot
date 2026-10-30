import unittest
from datetime import datetime, timedelta, timezone

import independent_model_evaluation as model_eval


def synthetic_samples(count=240):
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    samples = []
    for index in range(count):
        outcome = 1 if index % 10 < 6 else 0
        noise = ((index % 7) - 3) * 0.008
        midpoint_delta = (0.38 if outcome else -0.38) + noise
        forecast_at = base + timedelta(days=2 * index)
        resolved_at = forecast_at + timedelta(hours=24)
        samples.append({
            "market_id": f"market-{index}",
            "forecast_timestamp": forecast_at.isoformat(),
            "resolved_at": resolved_at.isoformat(),
            "market_probability_yes": 0.5,
            "outcome_yes": outcome,
            "features": {
                "logit_market_probability": 0.0,
                "book_midpoint_minus_market": midpoint_delta,
                "book_usable": 1.0,
                "depth_imbalance_top5": 0.75 if outcome else -0.75,
                "log_spread_bps": 4.5,
                "log_news_matches_24h": 1.0 + (0.2 if outcome else 0.0),
                "mean_lexical_overlap": 0.7 if outcome else 0.2,
                "log_volume_24h": 6.0,
            },
        })
    return samples


class FeatureModelEvaluationTests(unittest.TestCase):
    def test_builds_one_nearest_pre_resolution_snapshot_per_market(self):
        resolved_at = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        target = resolved_at - timedelta(hours=24)
        features = [
            {
                "market_id": "m1",
                "observed_at": (target - timedelta(hours=2)).isoformat(),
                "gamma_yes_probability": 0.4,
                "clob_book_usable": True,
                "clob_midpoint": 0.45,
                "clob_depth_imbalance_top5": 0.2,
                "clob_spread_bps": 300,
                "news_match_count_24h": 2,
                "news_headlines": [{"lexical_overlap": 0.75}],
                "volume_24h": 1000,
            },
            {
                "market_id": "m1",
                "observed_at": (target + timedelta(hours=1)).isoformat(),
                "gamma_yes_probability": 0.6,
                "clob_book_usable": False,
                "news_match_count_24h": 0,
            },
            {
                "market_id": "m1",
                "observed_at": (resolved_at + timedelta(minutes=1)).isoformat(),
                "gamma_yes_probability": 0.99,
            },
        ]
        resolutions = [{
            "market_id": "m1", "resolved_yes": 1, "resolved_at": resolved_at.isoformat(),
            "question": "Test market",
        }]
        samples = model_eval.build_feature_forecast_samples(features, resolutions)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["forecast_timestamp"], (target + timedelta(hours=1)).isoformat())
        self.assertEqual(samples[0]["market_probability_yes"], 0.6)
        self.assertEqual(samples[0]["outcome_yes"], 1)
        self.assertAlmostEqual(samples[0]["features"]["book_usable"], 0.0)
        self.assertAlmostEqual(samples[0]["features"]["log_news_matches_24h"], 0.0)

    def test_feature_model_beats_market_baseline_on_synthetic_signal(self):
        result = model_eval.evaluate_feature_model(synthetic_samples())
        self.assertEqual(result["status"], "scored")
        self.assertGreaterEqual(result["train_n"], 80)
        self.assertGreaterEqual(result["test_n"], 20)
        self.assertLess(result["feature_model"]["brier_score"], result["market_baseline"]["brier_score"])
        self.assertLess(result["log_loss_change_vs_market"], 0.0)
        self.assertEqual(
            result["label_availability_check"],
            "all training outcomes resolved before first test forecast",
        )

    def test_purges_training_labels_not_yet_known_at_test_start(self):
        samples = synthetic_samples()
        # The final row in the initial chronological training segment is deliberately
        # assigned a resolution after the first test forecast; it must be purged.
        split = int(len(samples) * 0.7)
        test_start = datetime.fromisoformat(samples[split]["forecast_timestamp"])
        samples[split - 1]["resolved_at"] = (test_start + timedelta(days=10)).isoformat()
        result = model_eval.evaluate_feature_model(samples)
        self.assertEqual(result["status"], "scored")
        self.assertGreaterEqual(result["purged_train_n"], 1)
        latest_train_resolved = datetime.fromisoformat(result["latest_training_label_available_at"])
        self.assertLess(latest_train_resolved, datetime.fromisoformat(result["test_forecast_start"]))

    def test_withholds_model_when_sample_is_too_small(self):
        result = model_eval.evaluate_feature_model(synthetic_samples(40))
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIsNone(result.get("feature_model"))

    def test_withholds_model_when_training_has_one_outcome_class(self):
        samples = synthetic_samples()
        split = int(len(samples) * 0.7)
        for row in samples[:split]:
            row["outcome_yes"] = 1
        result = model_eval.evaluate_feature_model(samples)
        self.assertEqual(result["status"], "insufficient_class_diversity")
        self.assertIsNone(result.get("feature_model"))

    def test_invalid_evaluation_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            model_eval.evaluate_feature_model([], train_fraction=0.95)
        with self.assertRaises(ValueError):
            model_eval.evaluate_feature_model([], minimum_train=1)
        with self.assertRaises(ValueError):
            model_eval.evaluate_feature_model([], l2=-1)


if __name__ == "__main__":
    unittest.main()
