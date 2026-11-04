"""Conservative, auditable governance for research-model candidates.

A promotion here only designates a research champion against the market-price
baseline. It does not change inference routing, portfolio decisions, or order
execution. The model must pass independent holdout, expanding-window folds,
paired uncertainty, and calibration checks before receiving that designation.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MIN_HOLDOUT_TRAIN_MARKETS = 80
MIN_HOLDOUT_TEST_MARKETS = 20
MIN_WALK_FORWARD_FOLDS = 3
MIN_WALK_FORWARD_TRAIN_PER_FOLD = 80
MIN_WALK_FORWARD_TEST_PER_FOLD = 20
MIN_POOLED_WALK_FORWARD_MARKETS = 90
MAX_ECE_DEGRADATION = 0.025


def _metric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def assess_research_candidate(
    evaluation: dict[str, Any],
    *,
    model_version: str,
    incumbent_version: str | None = None,
) -> dict[str, Any]:
    """Assess a candidate; missing, malformed, or weak evidence fails closed."""
    result = evaluation if isinstance(evaluation, dict) else {}
    version = str(model_version or "").strip()[:120]
    reasons: list[str] = []

    if not version:
        reasons.append("missing_model_version")
    if result.get("status") != "scored":
        reasons.append("holdout_not_scored")

    train_n = result.get("train_n")
    test_n = result.get("test_n")
    if not isinstance(train_n, int) or isinstance(train_n, bool) or train_n < MIN_HOLDOUT_TRAIN_MARKETS:
        reasons.append("holdout_training_sample_below_minimum")
    if not isinstance(test_n, int) or isinstance(test_n, bool) or test_n < MIN_HOLDOUT_TEST_MARKETS:
        reasons.append("holdout_test_sample_below_minimum")

    holdout_model = result.get("feature_model")
    holdout_market = result.get("market_baseline")
    if not isinstance(holdout_model, dict) or not isinstance(holdout_market, dict):
        holdout_model, holdout_market = {}, {}
        reasons.append("holdout_metrics_missing")

    model_brier = _metric(holdout_model.get("brier_score"))
    market_brier = _metric(holdout_market.get("brier_score"))
    model_log_loss = _metric(holdout_model.get("log_loss"))
    market_log_loss = _metric(holdout_market.get("log_loss"))
    if None in (model_brier, market_brier, model_log_loss, market_log_loss):
        reasons.append("holdout_score_metrics_missing")
    else:
        if model_brier >= market_brier:
            reasons.append("holdout_brier_not_better_than_market")
        if model_log_loss >= market_log_loss:
            reasons.append("holdout_log_loss_not_better_than_market")

    model_calibration = holdout_model.get("calibration")
    market_calibration = holdout_market.get("calibration")
    model_ece = _metric(
        model_calibration.get("expected_calibration_error")
        if isinstance(model_calibration, dict) else None
    )
    market_ece = _metric(
        market_calibration.get("expected_calibration_error")
        if isinstance(market_calibration, dict) else None
    )
    if model_ece is None or market_ece is None:
        reasons.append("holdout_calibration_metrics_missing")
    elif model_ece > market_ece + MAX_ECE_DEGRADATION:
        reasons.append("calibration_degradation_exceeds_tolerance")

    walk = result.get("walk_forward_validation")
    if not isinstance(walk, dict) or walk.get("status") != "scored":
        walk = {}
        reasons.append("walk_forward_not_scored")

    folds = walk.get("folds")
    if not isinstance(folds, list) or len(folds) < MIN_WALK_FORWARD_FOLDS:
        folds = []
        reasons.append("insufficient_walk_forward_folds")

    scored_folds = [row for row in folds if isinstance(row, dict) and row.get("status") == "scored"]
    if len(scored_folds) != len(folds) or len(scored_folds) < MIN_WALK_FORWARD_FOLDS:
        reasons.append("walk_forward_fold_coverage_incomplete")

    brier_wins = 0
    log_loss_wins = 0
    for fold in scored_folds:
        fold_train = fold.get("train_n")
        fold_test = fold.get("test_n")
        if (
            not isinstance(fold_train, int) or isinstance(fold_train, bool)
            or fold_train < MIN_WALK_FORWARD_TRAIN_PER_FOLD
        ):
            reasons.append("walk_forward_training_sample_below_minimum")
        if (
            not isinstance(fold_test, int) or isinstance(fold_test, bool)
            or fold_test < MIN_WALK_FORWARD_TEST_PER_FOLD
        ):
            reasons.append("walk_forward_test_sample_below_minimum")
        brier_delta = _metric(fold.get("brier_improvement_vs_market"))
        log_delta = _metric(fold.get("log_loss_change_vs_market"))
        if brier_delta is not None and brier_delta > 0:
            brier_wins += 1
        if log_delta is not None and log_delta < 0:
            log_loss_wins += 1

    if scored_folds:
        if brier_wins != len(scored_folds):
            reasons.append("brier_not_better_in_every_walk_forward_fold")
        required_log_wins = math.ceil(len(scored_folds) * 0.75)
        if log_loss_wins < required_log_wins:
            reasons.append("log_loss_improvement_not_consistent_across_folds")

    if walk.get("multi_fold_consistency_gate_passed") is not True:
        reasons.append("existing_walk_forward_consistency_gate_not_passed")

    pooled = walk.get("pooled_out_of_sample")
    pooled = pooled if isinstance(pooled, dict) else {}
    pooled_n = pooled.get("markets")
    if (
        not isinstance(pooled_n, int) or isinstance(pooled_n, bool)
        or pooled_n < MIN_POOLED_WALK_FORWARD_MARKETS
    ):
        reasons.append("pooled_walk_forward_sample_below_minimum")

    uncertainty = walk.get("paired_bootstrap_uncertainty")
    uncertainty = uncertainty if isinstance(uncertainty, dict) else {}
    if uncertainty.get("brier_ci_lower_above_zero") is not True:
        reasons.append("brier_improvement_confidence_interval_includes_zero")
    if uncertainty.get("log_loss_ci_lower_above_zero") is not True:
        reasons.append("log_loss_improvement_confidence_interval_includes_zero")

    # Preserve order while removing duplicate reason codes from multiple folds.
    reasons = list(dict.fromkeys(reasons))
    passed = not reasons
    incumbent = str(incumbent_version or "").strip() or "market-baseline-v1"
    if not passed:
        decision = "candidate_withheld"
    elif version == incumbent:
        decision = "already_research_champion"
    else:
        decision = "promoted_to_research_champion"

    return {
        "candidate_model_version": version or None,
        "incumbent_model_version": incumbent,
        "decision": decision,
        "promotion_gate_passed": passed,
        "reason_codes": reasons,
        "evidence": {
            "holdout_train_markets": train_n if isinstance(train_n, int) else None,
            "holdout_test_markets": test_n if isinstance(test_n, int) else None,
            "holdout_model_brier": model_brier,
            "holdout_market_brier": market_brier,
            "holdout_model_log_loss": model_log_loss,
            "holdout_market_log_loss": market_log_loss,
            "holdout_model_ece": model_ece,
            "holdout_market_ece": market_ece,
            "walk_forward_folds_scored": len(scored_folds),
            "walk_forward_brier_wins": brier_wins,
            "walk_forward_log_loss_wins": log_loss_wins,
            "pooled_walk_forward_markets": pooled_n if isinstance(pooled_n, int) else None,
            "brier_ci_lower_above_zero": uncertainty.get("brier_ci_lower_above_zero") is True,
            "log_loss_ci_lower_above_zero": uncertainty.get("log_loss_ci_lower_above_zero") is True,
        },
        "policy": {
            "minimum_holdout_train_markets": MIN_HOLDOUT_TRAIN_MARKETS,
            "minimum_holdout_test_markets": MIN_HOLDOUT_TEST_MARKETS,
            "minimum_walk_forward_folds": MIN_WALK_FORWARD_FOLDS,
            "minimum_walk_forward_train_per_fold": MIN_WALK_FORWARD_TRAIN_PER_FOLD,
            "minimum_walk_forward_test_per_fold": MIN_WALK_FORWARD_TEST_PER_FOLD,
            "minimum_pooled_walk_forward_markets": MIN_POOLED_WALK_FORWARD_MARKETS,
            "maximum_ece_degradation": MAX_ECE_DEGRADATION,
            "requires_positive_paired_95pct_brier_and_log_loss_intervals": True,
        },
        "promotion_scope": "research_champion_only; does_not_change_forecasts_or_enable_orders",
        "research_only": True,
        "live_execution_enabled": False,
        "execution": "none",
    }


class ModelGovernanceLedger:
    """Append-only governance history with restart-safe research champion state."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._records = self._load()
        self._fingerprints = {
            str(row.get("evaluation_fingerprint"))
            for row in self._records if row.get("evaluation_fingerprint")
        }

    def _load(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.path.exists():
            return records
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(row, dict):
                        records.append(row)
        except OSError:
            return []
        return records[-10000:]

    @staticmethod
    def _fingerprint(evaluation: dict[str, Any], model_version: str) -> str:
        walk = evaluation.get("walk_forward_validation")
        if not isinstance(walk, dict):
            walk = {}
        pooled = walk.get("pooled_out_of_sample")
        if not isinstance(pooled, dict):
            pooled = {}
        uncertainty = walk.get("paired_bootstrap_uncertainty")
        if not isinstance(uncertainty, dict):
            uncertainty = {}
        model = evaluation.get("feature_model")
        baseline = evaluation.get("market_baseline")
        model = model if isinstance(model, dict) else {}
        baseline = baseline if isinstance(baseline, dict) else {}
        material = {
            "model_version": model_version,
            "status": evaluation.get("status"),
            "train_n": evaluation.get("train_n"),
            "test_n": evaluation.get("test_n"),
            "model_brier": model.get("brier_score"),
            "market_brier": baseline.get("brier_score"),
            "model_log_loss": model.get("log_loss"),
            "market_log_loss": baseline.get("log_loss"),
            "model_calibration": model.get("calibration"),
            "market_calibration": baseline.get("calibration"),
            "dataset_provenance": evaluation.get("dataset_provenance"),
            "test_forecast_start": evaluation.get("test_forecast_start"),
            "latest_training_label_available_at": evaluation.get("latest_training_label_available_at"),
            "horizon_hours": evaluation.get("horizon_hours"),
            "tolerance_hours": evaluation.get("tolerance_hours"),
            "walk_status": walk.get("status"),
            "walk_folds": [
                {
                    "status": row.get("status"),
                    "train_n": row.get("train_n"),
                    "test_n": row.get("test_n"),
                    "brier": row.get("brier_improvement_vs_market"),
                    "log_loss": row.get("log_loss_change_vs_market"),
                    "train_start": row.get("train_start"),
                    "train_end": row.get("train_end"),
                    "test_forecast_start": row.get("test_forecast_start"),
                    "test_forecast_end": row.get("test_forecast_end"),
                    "latest_training_label_available_at": row.get("latest_training_label_available_at"),
                }
                for row in walk.get("folds", []) if isinstance(row, dict)
            ],
            "pooled_n": pooled.get("markets"),
            "bootstrap": {
                "brier_ci": uncertainty.get("brier_improvement_ci95"),
                "log_loss_ci": uncertainty.get("log_loss_improvement_ci95"),
            },
        }
        serialized = json.dumps(material, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def assess_and_record(
        self,
        evaluation: dict[str, Any],
        *,
        model_version: str,
    ) -> dict[str, Any]:
        version = str(model_version or "").strip()
        with self._lock:
            incumbent = self._current_champion_version()
            assessment = assess_research_candidate(
                evaluation, model_version=version, incumbent_version=incumbent
            )
            fingerprint = self._fingerprint(
                evaluation if isinstance(evaluation, dict) else {}, version
            )
            if fingerprint in self._fingerprints:
                assessment["audit_record_written"] = False
                assessment["duplicate_evaluation"] = True
                return assessment

            record = {
                **assessment,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "evaluation_fingerprint": fingerprint,
                "evaluation_provenance": (
                    evaluation.get("dataset_provenance")
                    if isinstance(evaluation, dict) else None
                ),
                "evaluation_window": {
                    "test_forecast_start": evaluation.get("test_forecast_start"),
                    "latest_training_label_available_at": evaluation.get("latest_training_label_available_at"),
                    "horizon_hours": evaluation.get("horizon_hours"),
                    "tolerance_hours": evaluation.get("tolerance_hours"),
                } if isinstance(evaluation, dict) else {},
                "audit_record_written": True,
                "duplicate_evaluation": False,
            }
            if assessment["decision"] == "promoted_to_research_champion":
                record["research_champion_model_version"] = version
            else:
                record["research_champion_model_version"] = incumbent
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                handle.flush()
            self._records.append(record)
            self._fingerprints.add(fingerprint)
            return record

    def _current_champion_version(self) -> str | None:
        for row in reversed(self._records):
            candidate = row.get("research_champion_model_version")
            if candidate:
                return str(candidate)
        return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            latest = self._records[-1] if self._records else None
            champion = self._current_champion_version()
            return {
                "governance_mode": "RESEARCH_CHAMPION_CHALLENGER_V1",
                "research_champion_model_version": champion,
                "incumbent_model_version": champion or "market-baseline-v1",
                "latest_candidate_decision": latest.get("decision") if latest else "awaiting_evaluation",
                "latest_candidate_model_version": latest.get("candidate_model_version") if latest else None,
                "latest_candidate_reason_codes": list(latest.get("reason_codes") or []) if latest else [],
                "governance_evaluations_retained": len(self._records),
                "promotion_scope": "research_champion_only; no forecast routing or order execution changes",
                "model_artifact_persisted": False,
                "live_execution_enabled": False,
                "research_only": True,
                "execution": "none",
            }
