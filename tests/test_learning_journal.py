import tempfile
import unittest
from pathlib import Path

from learning_journal import LearningJournal


class LearningJournalTests(unittest.TestCase):
    def forecast(self, market_id, version, observed_at, model_probability=0.8, market_probability=0.5):
        return {
            "market_id": market_id,
            "question": "Will this research event happen?",
            "observed_at": observed_at,
            "feature_age_seconds": 60,
            "feature_model_probability_yes": model_probability,
            "market_probability_yes": market_probability,
        }

    def test_records_forecasts_once_per_model_market_and_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = LearningJournal(root / "forecasts.jsonl", root / "feedback.jsonl")
            row = self.forecast("market-1", "v1", "2026-01-01T00:00:00Z")
            first = journal.record_forecasts([row], model_version="v1")
            second = journal.record_forecasts([row], model_version="v1")
            self.assertEqual(first["added"], 1)
            self.assertEqual(second["added"], 0)
            self.assertEqual(len((root / "forecasts.jsonl").read_text().splitlines()), 1)

    def test_scores_nearest_pre_resolution_forecast_against_market_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = LearningJournal(root / "forecasts.jsonl", root / "feedback.jsonl")
            forecasts = [
                self.forecast("market-1", "v1", "2026-01-01T00:00:00Z", 0.8, 0.5),
                self.forecast("market-1", "v1", "2025-12-31T23:00:00Z", 0.2, 0.5),
                self.forecast("market-1", "v1", "2026-01-02T01:00:00Z", 0.99, 0.5),
            ]
            journal.record_forecasts(forecasts, model_version="v1")
            resolutions = [{
                "market_id": "market-1",
                "resolved_yes": 1,
                "resolved_at": "2026-01-02T00:00:00Z",
            }]
            first = journal.score_resolved_forecasts(resolutions)
            second = journal.score_resolved_forecasts(resolutions)
            self.assertEqual(first["scored_added"], 1)
            self.assertEqual(second["scored_added"], 0)
            summary = journal.summary()
            row = summary["performance_by_model_version"][0]
            self.assertEqual(row["model_version"], "v1")
            self.assertEqual(row["n"], 1)
            self.assertAlmostEqual(row["model_brier_score"], 0.04)
            self.assertAlmostEqual(row["market_brier_score"], 0.25)
            self.assertAlmostEqual(row["brier_improvement_vs_market"], 0.21)

    def test_requires_fixed_horizon_tolerance_and_never_uses_post_resolution_forecasts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = LearningJournal(root / "forecasts.jsonl", root / "feedback.jsonl")
            forecasts = [
                self.forecast("market-1", "v1", "2025-12-30T00:00:00Z"),
                self.forecast("market-1", "v1", "2026-01-02T01:00:00Z"),
            ]
            journal.record_forecasts(forecasts, model_version="v1")
            result = journal.score_resolved_forecasts([{
                "market_id": "market-1",
                "resolved_yes": 1,
                "resolved_at": "2026-01-02T00:00:00Z",
            }])
            self.assertEqual(result["scored_added"], 0)

    def test_keeps_model_versions_separate_and_stores_evaluation_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = LearningJournal(root / "forecasts.jsonl", root / "feedback.jsonl", root / "evaluations.jsonl")
            journal.record_forecasts(
                [self.forecast("market-1", "v1", "2026-01-01T00:00:00Z")],
                model_version="v1",
            )
            journal.record_forecasts(
                [self.forecast("market-1", "v2", "2026-01-01T00:00:00Z", 0.7, 0.5)],
                model_version="v2",
            )
            resolutions = [{
                "market_id": "market-1",
                "resolved_yes": 1,
                "resolved_at": "2026-01-02T00:00:00Z",
            }]
            journal.score_resolved_forecasts(resolutions)
            summary = journal.summary()
            self.assertEqual(summary["model_versions_with_feedback"], 2)

            evaluation = journal.record_evaluation({
                "status": "scored",
                "eligible_markets": 150,
                "train_n": 100,
                "test_n": 30,
                "walk_forward_validation": {
                    "status": "scored",
                    "folds_scored": 3,
                    "multi_fold_consistency_gate_passed": False,
                    "pooled_out_of_sample": {
                        "feature_model": {"brier_score": 0.21},
                        "market_baseline": {"brier_score": 0.22},
                    },
                },
            })
            self.assertEqual(evaluation["status"], "scored")
            self.assertFalse(evaluation["multi_fold_consistency_gate_passed"])
            self.assertFalse(summary["live_execution_enabled"])
            self.assertFalse(summary["automatic_parameter_tuning"])


if __name__ == "__main__":
    unittest.main()
