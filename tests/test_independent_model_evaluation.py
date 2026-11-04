import math
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
    def test_score_reports_calibration_for_well_calibrated_probabilities(self):
        probabilities = [0.2] * 10 + [0.8] * 10
        labels = [1, 1] + [0] * 8 + [1] * 8 + [0, 0]
        result = model_eval._score(probabilities, labels)

        self.assertEqual(result["n"], 20)
        calibration = result["calibration"]
        self.assertEqual(calibration["method"], "equal_width_10_bins")
        self.assertAlmostEqual(calibration["expected_calibration_error"], 0.0)
        self.assertAlmostEqual(calibration["maximum_calibration_error"], 0.0)
        self.assertEqual(len(calibration["bins"]), 2)
        self.assertEqual(calibration["bins"][0]["count"], 10)
        self.assertEqual(calibration["bins"][1]["count"], 10)

    def test_score_detects_overconfident_probability_calibration(self):
        result = model_eval._score([0.9] * 10, [1, 1, 1, 1, 1, 0, 0, 0, 0, 0])
        self.assertAlmostEqual(result["calibration"]["expected_calibration_error"], 0.4)
        self.assertAlmostEqual(result["calibration"]["maximum_calibration_error"], 0.4)
        self.assertEqual(result["calibration"]["bins"][0]["count"], 10)

    def test_score_rejects_nonfinite_or_out_of_range_probabilities(self):
        for probability in (float("nan"), float("inf"), -0.01, 1.01):
            with self.subTest(probability=probability):
                with self.assertRaises(ValueError):
                    model_eval._score([probability], [1])

    def test_holdout_excludes_all_duplicate_market_samples(self):
        samples = synthetic_samples()
        duplicate = {
            **samples[0],
            "forecast_timestamp": (
                datetime.fromisoformat(samples[0]["forecast_timestamp"]) + timedelta(hours=1)
            ).isoformat(),
            "resolved_at": (
                datetime.fromisoformat(samples[0]["resolved_at"]) + timedelta(hours=1)
            ).isoformat(),
        }
        result = model_eval.evaluate_feature_model(samples + [duplicate])
        self.assertEqual(result["status"], "scored")
        self.assertEqual(result["eligible_markets"], len(samples) - 1)
        self.assertEqual(result["duplicate_market_ids_excluded"], 1)
        self.assertEqual(result["duplicate_market_samples_excluded"], 2)

    def test_walk_forward_deduplication_is_independent_of_input_order(self):
        samples = synthetic_samples()
        duplicate = {
            **samples[0],
            "forecast_timestamp": (
                datetime.fromisoformat(samples[0]["forecast_timestamp"]) + timedelta(hours=1)
            ).isoformat(),
            "resolved_at": (
                datetime.fromisoformat(samples[0]["resolved_at"]) + timedelta(hours=1)
            ).isoformat(),
        }
        result = model_eval.evaluate_feature_model_walk_forward([duplicate] + samples)
        self.assertEqual(result["status"], "scored")
        self.assertEqual(result["eligible_unique_markets"], len(samples) - 1)
        self.assertEqual(result["duplicate_market_ids_excluded"], 1)
        self.assertEqual(result["duplicate_market_samples_excluded"], 2)

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

    def test_live_forecasts_train_only_on_resolved_history_and_skip_old_rows(self):
        now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        feature_rows = []
        resolutions = []
        for index in range(100):
            outcome = 1 if index % 2 == 0 else 0
            resolved_at = now - timedelta(days=2, minutes=index)
            feature_rows.append({
                "market_id": f"history-{index}",
                "observed_at": (resolved_at - timedelta(hours=24)).isoformat(),
                "gamma_yes_probability": 0.5,
                "clob_book_usable": True,
                "clob_midpoint": 0.85 if outcome else 0.15,
                "clob_depth_imbalance_top5": 0.7 if outcome else -0.7,
                "clob_spread_bps": 120,
                "news_match_count_24h": 2 if outcome else 0,
                "news_headlines": [{"lexical_overlap": 0.8 if outcome else 0.1}],
                "volume_24h": 10000,
            })
            resolutions.append({
                "market_id": f"history-{index}",
                "resolved_yes": outcome,
                "resolved_at": resolved_at.isoformat(),
            })
        feature_rows.extend([
            {
                "market_id": "live-positive",
                "question": "Current positive feature market",
                "observed_at": (now - timedelta(minutes=15)).isoformat(),
                "gamma_yes_probability": 0.5,
                "clob_book_usable": True,
                "clob_midpoint": 0.85,
                "clob_depth_imbalance_top5": 0.7,
                "clob_spread_bps": 120,
                "news_match_count_24h": 2,
                "news_headlines": [{"lexical_overlap": 0.8}],
                "volume_24h": 10000,
            },
            {
                "market_id": "live-negative",
                "question": "Current negative feature market",
                "observed_at": (now - timedelta(minutes=15)).isoformat(),
                "gamma_yes_probability": 0.5,
                "clob_book_usable": True,
                "clob_midpoint": 0.15,
                "clob_depth_imbalance_top5": -0.7,
                "clob_spread_bps": 120,
                "news_match_count_24h": 0,
                "news_headlines": [{"lexical_overlap": 0.1}],
                "volume_24h": 10000,
            },
            {
                "market_id": "live-stale",
                "observed_at": (now - timedelta(hours=3)).isoformat(),
                "gamma_yes_probability": 0.5,
                "clob_book_usable": False,
                "volume_24h": 100,
            },
        ])
        result = model_eval.forecast_current_feature_rows(
            feature_rows, resolutions, now=now, max_feature_age_seconds=3600
        )
        self.assertEqual(result["status"], "model_fitted")
        self.assertTrue(result["model_version"].startswith("feature-logistic-v3-"))
        self.assertEqual(result["model"]["version"], result["model_version"])
        self.assertTrue(all(row["model_version"] == result["model_version"] for row in result["forecasts"]))
        self.assertEqual(result["training"]["eligible_unique_markets"], 100)
        self.assertEqual(result["training"]["outcome_yes_count"], 50)
        self.assertEqual(result["current_market_coverage"]["fresh_unresolved_markets"], 2)
        self.assertEqual(result["current_market_coverage"]["exclusions"]["stale_rows"], 1)
        self.assertEqual(len(result["forecasts"]), 2)
        self.assertTrue(all(
            0.0 <= row["feature_model_probability_yes"] <= 1.0
            for row in result["forecasts"]
        ))
        self.assertEqual(result["decision"], "NO_ACTION_RESEARCH_ONLY")
        self.assertFalse(result["live_execution_enabled"])

    def test_extract_model_features_requires_quality_eligible_fresh_orderbook(self):
        approved = model_eval.extract_model_features({
            "gamma_yes_probability": 0.5,
            "clob_book_usable": True,
            "clob_book_quality_eligible": True,
            "clob_book_age_seconds": 30,
            "clob_midpoint": 0.6,
            "clob_spread_bps": 200,
            "clob_depth_imbalance_top5": 0.4,
        })
        self.assertEqual(approved["book_usable"], 1.0)
        self.assertAlmostEqual(approved["book_midpoint_minus_market"], 0.1)
        self.assertAlmostEqual(approved["log_clob_book_age_seconds"], math.log1p(30))

        stale_or_rejected = model_eval.extract_model_features({
            "gamma_yes_probability": 0.5,
            "clob_book_usable": True,
            "clob_book_quality_eligible": False,
            "clob_book_age_seconds": 600,
            "clob_midpoint": 0.9,
            "clob_spread_bps": 10,
            "clob_depth_imbalance_top5": 1.0,
        })
        self.assertEqual(stale_or_rejected["book_usable"], 0.0)
        self.assertIsNone(stale_or_rejected["book_midpoint_minus_market"])
        self.assertIsNone(stale_or_rejected["log_spread_bps"])
        self.assertIsNone(stale_or_rejected["depth_imbalance_top5"])
        self.assertIsNone(stale_or_rejected["log_clob_book_age_seconds"])

    def test_extract_model_features_fails_closed_when_book_quality_metadata_is_missing(self):
        features = model_eval.extract_model_features({
            "gamma_yes_probability": 0.5,
            "clob_book_usable": True,
            "clob_book_age_seconds": 5,
            "clob_midpoint": 0.7,
            "clob_spread_bps": 100,
            "clob_depth_imbalance_top5": 0.5,
        })
        self.assertEqual(features["book_usable"], 0.0)
        self.assertIsNone(features["book_midpoint_minus_market"])

    def test_extract_model_features_includes_market_aware_news_diagnostics(self):
        features = model_eval.extract_model_features({
            "gamma_yes_probability": 0.5,
            "news_match_count_24h": 3,
            "news_topic_category_count_24h": 4,
            "news_unique_source_label_count_24h": 3,
            "news_unique_publisher_count_24h": 2,
            "news_mean_relevance_score_24h": 0.7,
            "news_max_relevance_score_24h": 0.9,
            "news_mean_headline_precision_24h": 0.8,
            "news_mean_age_hours_24h": 2.5,
            "volume_24h": 100,
        })
        self.assertAlmostEqual(features["log_news_topic_category_count_24h"], math.log1p(4))
        self.assertAlmostEqual(features["log_news_unique_source_label_count_24h"], math.log1p(3))
        self.assertAlmostEqual(features["log_news_unique_publisher_count_24h"], math.log1p(2))
        self.assertEqual(features["mean_news_relevance_score_24h"], 0.7)
        self.assertEqual(features["max_news_relevance_score_24h"], 0.9)
        self.assertEqual(features["mean_news_headline_precision_24h"], 0.8)
        self.assertEqual(features["mean_news_age_hours_24h"], 2.5)

    def test_extract_model_features_falls_back_for_legacy_source_counts_and_headline_metrics(self):
        features = model_eval.extract_model_features({
            "gamma_yes_probability": 0.5,
            "news_unique_source_label_count_24h": 2,
            "news_headlines": [
                {
                    "relevance_score": 0.6,
                    "headline_precision": 0.8,
                    "age_hours": 1.0,
                },
                {
                    "relevance_score": 0.8,
                    "headline_precision": 0.6,
                    "age_hours": 3.0,
                },
            ],
        })
        self.assertAlmostEqual(features["log_news_unique_publisher_count_24h"], math.log1p(2))
        self.assertEqual(features["mean_news_relevance_score_24h"], 0.7)
        self.assertEqual(features["max_news_relevance_score_24h"], 0.8)
        self.assertEqual(features["mean_news_headline_precision_24h"], 0.7)
        self.assertEqual(features["mean_news_age_hours_24h"], 2.0)

    def test_model_version_changes_when_resolved_training_history_changes(self):
        now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        feature_rows = []
        resolutions = []
        for index in range(100):
            outcome = index % 2
            resolved_at = now - timedelta(days=2, minutes=index)
            feature_rows.append({
                "market_id": f"history-{index}",
                "observed_at": (resolved_at - timedelta(hours=24)).isoformat(),
                "gamma_yes_probability": 0.5,
                "clob_book_usable": True,
                "clob_midpoint": 0.65 if outcome else 0.35,
                "clob_depth_imbalance_top5": 0.5 if outcome else -0.5,
                "news_match_count_24h": 1,
                "news_topic_category_count_24h": 1,
                "news_unique_source_label_count_24h": 1,
                "volume_24h": 1000,
            })
            resolutions.append({
                "market_id": f"history-{index}",
                "resolved_yes": outcome,
                "resolved_at": resolved_at.isoformat(),
            })
        first = model_eval.forecast_current_feature_rows(feature_rows, resolutions, now=now)
        changed_resolutions = [dict(row) for row in resolutions]
        changed_resolutions[0] = {**changed_resolutions[0], "resolved_yes": 1}
        second = model_eval.forecast_current_feature_rows(feature_rows, changed_resolutions, now=now)
        self.assertNotEqual(first["model_version"], second["model_version"])

    def test_live_forecasts_withhold_model_until_enough_labels_resolve(self):
        now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        feature_rows = []
        resolutions = []
        for index in range(79):
            outcome = index % 2
            resolved_at = now - timedelta(days=2, minutes=index)
            feature_rows.append({
                "market_id": f"history-{index}",
                "observed_at": (resolved_at - timedelta(hours=24)).isoformat(),
                "gamma_yes_probability": 0.5,
                "clob_book_usable": True,
                "clob_midpoint": 0.6 if outcome else 0.4,
                "volume_24h": 1000,
            })
            resolutions.append({
                "market_id": f"history-{index}",
                "resolved_yes": outcome,
                "resolved_at": resolved_at.isoformat(),
            })
        result = model_eval.forecast_current_feature_rows(
            feature_rows, resolutions, now=now
        )
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["training"]["eligible_unique_markets"], 79)
        self.assertEqual(result["forecasts"], [])
        self.assertEqual(result["decision"], "NO_ACTION_RESEARCH_ONLY")

    def test_live_forecasts_never_use_unresolved_future_labels(self):
        now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        features = []
        resolutions = []
        for index in range(100):
            outcome = index % 2
            resolved_at = now - timedelta(days=2) if index < 80 else now + timedelta(days=2)
            features.append({
                "market_id": f"market-{index}",
                "observed_at": (resolved_at - timedelta(hours=24)).isoformat(),
                "gamma_yes_probability": 0.5,
                "clob_book_usable": True,
                "clob_midpoint": 0.65 if outcome else 0.35,
                "volume_24h": 1000,
            })
            resolutions.append({
                "market_id": f"market-{index}",
                "resolved_yes": outcome,
                "resolved_at": resolved_at.isoformat(),
            })
        result = model_eval.forecast_current_feature_rows(features, resolutions, now=now)
        self.assertEqual(result["status"], "model_fitted_no_fresh_markets")
        self.assertEqual(result["training"]["eligible_unique_markets"], 80)
        self.assertEqual(result["forecasts"], [])

    def test_dataset_provenance_is_deterministic_and_sensitive_to_selected_features(self):
        samples = synthetic_samples()
        first = model_eval.build_evaluation_dataset_provenance(samples)
        reordered = model_eval.build_evaluation_dataset_provenance(list(reversed(samples)))
        self.assertEqual(first["fingerprint"], reordered["fingerprint"])
        self.assertEqual(first["samples_n"], len(samples))
        self.assertEqual(first["feature_names"], list(model_eval.FEATURE_NAMES))
        changed = [dict(row, features=dict(row["features"])) for row in samples]
        changed[0]["features"]["log_volume_24h"] += 0.25
        second = model_eval.build_evaluation_dataset_provenance(changed)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        changed_label = [dict(row) for row in samples]
        changed_label[0]["outcome_yes"] = 1 - changed_label[0]["outcome_yes"]
        third = model_eval.build_evaluation_dataset_provenance(changed_label)
        self.assertNotEqual(first["fingerprint"], third["fingerprint"])

    def test_invalid_evaluation_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            model_eval.evaluate_feature_model([], train_fraction=0.95)
        with self.assertRaises(ValueError):
            model_eval.evaluate_feature_model([], minimum_train=1)
        with self.assertRaises(ValueError):
            model_eval.evaluate_feature_model([], l2=-1)



    def test_paired_bootstrap_is_deterministic_and_reports_improvement_intervals(self):
        labels = [1, 0, 1, 0] * 15
        market = [0.5] * len(labels)
        model_probabilities = [0.9 if label else 0.1 for label in labels]
        first = model_eval.paired_bootstrap_model_improvement(
            labels, market, model_probabilities, replicates=500
        )
        second = model_eval.paired_bootstrap_model_improvement(
            labels, market, model_probabilities, replicates=500
        )
        self.assertEqual(first, second)
        self.assertGreater(first["brier_improvement_vs_market"], 0.0)
        self.assertGreater(first["log_loss_improvement_vs_market"], 0.0)
        self.assertTrue(first["both_score_improvement_intervals_above_zero"])
        self.assertEqual(first["sample_n"], len(labels))

    def test_paired_bootstrap_rejects_misaligned_and_invalid_samples(self):
        with self.assertRaises(ValueError):
            model_eval.paired_bootstrap_model_improvement([1], [0.5, 0.6], [0.7])
        with self.assertRaises(ValueError):
            model_eval.paired_bootstrap_model_improvement([2, 0], [0.5, 0.5], [0.6, 0.4])
        with self.assertRaises(ValueError):
            model_eval.paired_bootstrap_model_improvement([1, 0], [0.5, float("nan")], [0.8, 0.2])

    def test_walk_forward_validation_requires_consistent_multi_fold_improvement(self):
        result = model_eval.evaluate_feature_model_walk_forward(synthetic_samples())
        self.assertEqual(result["status"], "scored")
        self.assertEqual(result["folds_scored"], 3)
        self.assertEqual(result["brier_improved_folds"], 3)
        self.assertEqual(result["log_loss_improved_folds"], 3)
        self.assertTrue(result["multi_fold_consistency_gate_passed"])
        self.assertEqual(result["pooled_out_of_sample"]["markets"], 96)
        self.assertTrue(
            result["paired_bootstrap_uncertainty"]["both_score_improvement_intervals_above_zero"]
        )
        self.assertEqual(result["preliminary_consistency_check"], "improved_across_all_folds")
        self.assertFalse(result["live_execution_enabled"])
        self.assertEqual(result["real_money_execution"], "not_implemented")
        for fold in result["folds"]:
            self.assertEqual(fold["status"], "scored")
            self.assertLess(
                datetime.fromisoformat(fold["latest_training_label_available_at"]),
                datetime.fromisoformat(fold["test_forecast_start"]),
            )

    def test_walk_forward_purges_training_labels_not_known_at_fold_start(self):
        samples = synthetic_samples()
        warmup_n = int(len(samples) * 0.6)
        test_start = datetime.fromisoformat(samples[warmup_n]["forecast_timestamp"])
        samples[warmup_n - 1]["resolved_at"] = (test_start + timedelta(days=5)).isoformat()
        result = model_eval.evaluate_feature_model_walk_forward(samples)
        self.assertEqual(result["status"], "scored")
        first_fold = result["folds"][0]
        self.assertGreaterEqual(first_fold["purged_train_n"], 1)
        self.assertLess(
            datetime.fromisoformat(first_fold["latest_training_label_available_at"]),
            datetime.fromisoformat(first_fold["test_forecast_start"]),
        )

    def test_walk_forward_withholds_all_folds_when_sample_is_too_small(self):
        result = model_eval.evaluate_feature_model_walk_forward(synthetic_samples(100))
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["folds"], [])
        self.assertEqual(result["status"], "insufficient_data")

    def test_walk_forward_withholds_fold_when_training_has_one_class(self):
        samples = synthetic_samples()
        warmup_n = int(len(samples) * 0.6)
        for row in samples[:warmup_n]:
            row["outcome_yes"] = 1
        result = model_eval.evaluate_feature_model_walk_forward(samples)
        self.assertEqual(result["status"], "insufficient_fold_coverage")
        self.assertEqual(result["folds"][0]["status"], "insufficient_class_diversity")
        self.assertFalse(result["live_execution_enabled"])

    def test_walk_forward_rejects_invalid_configuration(self):
        with self.assertRaises(ValueError):
            model_eval.evaluate_feature_model_walk_forward([], folds=2)
        with self.assertRaises(ValueError):
            model_eval.evaluate_feature_model_walk_forward([], warmup_fraction=0.9)
        with self.assertRaises(ValueError):
            model_eval.evaluate_feature_model_walk_forward([], l2=-0.1)

if __name__ == "__main__":
    unittest.main()
