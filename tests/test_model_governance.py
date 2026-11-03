import tempfile
import unittest
from pathlib import Path

from model_governance import ModelGovernanceLedger, assess_research_candidate


def strong_evaluation():
    folds = []
    for index in range(3):
        folds.append({
            "fold": index + 1,
            "status": "scored",
            "train_n": 100 + index * 20,
            "test_n": 30,
            "brier_improvement_vs_market": 0.025,
            "log_loss_change_vs_market": -0.05,
        })
    return {
        "status": "scored",
        "train_n": 120,
        "test_n": 40,
        "feature_model": {
            "brier_score": 0.15,
            "log_loss": 0.48,
            "calibration": {"expected_calibration_error": 0.025},
        },
        "market_baseline": {
            "brier_score": 0.20,
            "log_loss": 0.69,
            "calibration": {"expected_calibration_error": 0.04},
        },
        "walk_forward_validation": {
            "status": "scored",
            "folds": folds,
            "folds_scored": 3,
            "multi_fold_consistency_gate_passed": True,
            "pooled_out_of_sample": {"markets": 90},
            "paired_bootstrap_uncertainty": {
                "sample_n": 90,
                "brier_ci_lower_above_zero": True,
                "log_loss_ci_lower_above_zero": True,
            },
        },
    }


class ModelGovernanceTests(unittest.TestCase):
    def test_candidate_passes_only_with_all_independent_gates(self):
        result = assess_research_candidate(
            strong_evaluation(), model_version="candidate-v2"
        )
        self.assertTrue(result["promotion_gate_passed"])
        self.assertEqual(result["decision"], "promoted_to_research_champion")
        self.assertEqual(result["reason_codes"], [])
        self.assertFalse(result["live_execution_enabled"])
        self.assertEqual(result["execution"], "none")

    def test_insufficient_samples_fail_closed(self):
        evaluation = strong_evaluation()
        evaluation["train_n"] = 79
        evaluation["walk_forward_validation"]["pooled_out_of_sample"]["markets"] = 89
        result = assess_research_candidate(evaluation, model_version="candidate-v2")
        self.assertFalse(result["promotion_gate_passed"])
        self.assertIn("holdout_training_sample_below_minimum", result["reason_codes"])
        self.assertIn("pooled_walk_forward_sample_below_minimum", result["reason_codes"])
        self.assertEqual(result["decision"], "candidate_withheld")

    def test_candidate_is_withheld_when_paired_uncertainty_includes_zero(self):
        evaluation = strong_evaluation()
        evaluation["walk_forward_validation"]["paired_bootstrap_uncertainty"][
            "brier_ci_lower_above_zero"
        ] = False
        result = assess_research_candidate(evaluation, model_version="candidate-v2")
        self.assertFalse(result["promotion_gate_passed"])
        self.assertIn(
            "brier_improvement_confidence_interval_includes_zero",
            result["reason_codes"],
        )

    def test_candidate_is_withheld_when_one_fold_does_not_improve_brier(self):
        evaluation = strong_evaluation()
        evaluation["walk_forward_validation"]["folds"][1][
            "brier_improvement_vs_market"
        ] = -0.001
        result = assess_research_candidate(evaluation, model_version="candidate-v2")
        self.assertFalse(result["promotion_gate_passed"])
        self.assertIn(
            "brier_not_better_in_every_walk_forward_fold",
            result["reason_codes"],
        )

    def test_candidate_is_withheld_for_material_calibration_degradation(self):
        evaluation = strong_evaluation()
        evaluation["feature_model"]["calibration"]["expected_calibration_error"] = 0.10
        result = assess_research_candidate(evaluation, model_version="candidate-v2")
        self.assertFalse(result["promotion_gate_passed"])
        self.assertIn(
            "calibration_degradation_exceeds_tolerance",
            result["reason_codes"],
        )

    def test_missing_evaluation_is_not_treated_as_a_pass(self):
        result = assess_research_candidate({}, model_version="candidate-v2")
        self.assertFalse(result["promotion_gate_passed"])
        self.assertEqual(result["decision"], "candidate_withheld")

    def test_ledger_persists_champion_and_keeps_it_when_new_candidate_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "governance.jsonl"
            ledger = ModelGovernanceLedger(path)
            promoted = ledger.assess_and_record(
                strong_evaluation(), model_version="candidate-v2"
            )
            self.assertEqual(promoted["decision"], "promoted_to_research_champion")
            self.assertEqual(
                ledger.snapshot()["research_champion_model_version"], "candidate-v2"
            )

            weak = strong_evaluation()
            weak["walk_forward_validation"]["multi_fold_consistency_gate_passed"] = False
            rejected = ledger.assess_and_record(weak, model_version="candidate-v3")
            self.assertEqual(rejected["decision"], "candidate_withheld")
            self.assertEqual(
                ledger.snapshot()["research_champion_model_version"], "candidate-v2"
            )

            restarted = ModelGovernanceLedger(path)
            self.assertEqual(
                restarted.snapshot()["research_champion_model_version"], "candidate-v2"
            )
            self.assertEqual(restarted.snapshot()["governance_evaluations_retained"], 2)

    def test_duplicate_evaluations_do_not_grow_audit_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "governance.jsonl"
            ledger = ModelGovernanceLedger(path)
            first = ledger.assess_and_record(
                strong_evaluation(), model_version="candidate-v2"
            )
            second = ledger.assess_and_record(
                strong_evaluation(), model_version="candidate-v2"
            )
            self.assertTrue(first["audit_record_written"])
            self.assertFalse(second["audit_record_written"])
            self.assertTrue(second["duplicate_evaluation"])
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
