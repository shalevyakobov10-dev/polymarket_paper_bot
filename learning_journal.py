"""Persistent research-only forecast ledger and realized-outcome feedback loop.

The journal records model forecasts before market resolution, then scores one
pre-resolution forecast per market/model version near a fixed 24-hour horizon.
It never places orders, changes strategy parameters, or authorizes live trading.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paper_trading import rotate_jsonl_file


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _rotation_paths(path: Path, backups: int = 5) -> list[Path]:
    bounded = min(10, max(0, int(backups)))
    return [
        path.with_name(f"{path.name}.{index}")
        for index in range(bounded, 0, -1)
    ] + [path]


def _read_jsonl(paths: list[Path], *, limit: int = 100_000) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(value, dict):
                        records.append(value)
        except OSError:
            continue
    return records[-max(1, int(limit)):]


class LearningJournal:
    """Append-only forecasts and outcome scores with stable idempotency keys."""

    def __init__(
        self,
        forecast_path: str | Path,
        feedback_path: str | Path,
        evaluation_path: str | Path | None = None,
    ) -> None:
        self.forecast_path = Path(forecast_path)
        self.feedback_path = Path(feedback_path)
        self.evaluation_path = (
            Path(evaluation_path)
            if evaluation_path is not None
            else self.feedback_path.with_name("model_learning_evaluations.jsonl")
        )
        self._lock = threading.RLock()
        self._forecast_ids: set[str] = set()
        self._feedback_ids: set[str] = set()
        self._load_ids()

    def _load_ids(self) -> None:
        for row in _read_jsonl(_rotation_paths(self.forecast_path)):
            if row.get("forecast_id"):
                self._forecast_ids.add(str(row["forecast_id"]))
        for row in _read_jsonl(_rotation_paths(self.feedback_path)):
            if row.get("forecast_id"):
                self._feedback_ids.add(str(row["forecast_id"]))

    @staticmethod
    def _stable_id(*parts: str) -> str:
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    def _append(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate_jsonl_file(path, 10 * 1024 * 1024, 5)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")

    def record_forecasts(
        self,
        forecasts: list[dict[str, Any]],
        *,
        model_version: str,
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        """Persist each model-version/market/feature-time forecast once."""
        version = str(model_version or "").strip()
        if not version:
            return {"added": 0, "skipped": len(forecasts or []), "reason": "missing_model_version"}
        generated = generated_at or datetime.now(timezone.utc).isoformat()
        added = 0
        skipped = 0
        with self._lock:
            for forecast in forecasts or []:
                if not isinstance(forecast, dict):
                    skipped += 1
                    continue
                market_id = str(forecast.get("market_id") or "").strip()
                observed = _timestamp(forecast.get("observed_at"))
                model_probability = _number(forecast.get("feature_model_probability_yes"))
                market_probability = _number(forecast.get("market_probability_yes"))
                if (
                    not market_id or observed is None
                    or model_probability is None or not 0.0 <= model_probability <= 1.0
                    or market_probability is None or not 0.0 <= market_probability <= 1.0
                ):
                    skipped += 1
                    continue
                observed_iso = _iso(observed)
                forecast_id = self._stable_id(version, market_id, observed_iso)
                if forecast_id in self._forecast_ids:
                    skipped += 1
                    continue
                record = {
                    "forecast_id": forecast_id,
                    "model_version": version,
                    "model_family": "regularized_logistic_regression",
                    "market_id": market_id,
                    "question": str(forecast.get("question") or "Unknown market")[:500],
                    "forecast_timestamp": observed_iso,
                    "generated_at": str(generated)[:80],
                    "feature_age_seconds": _number(forecast.get("feature_age_seconds")),
                    "market_probability_yes": round(market_probability, 10),
                    "model_probability_yes": round(model_probability, 10),
                    "model_minus_market_probability_points": round(
                        (model_probability - market_probability) * 100.0, 6
                    ),
                    "research_only": True,
                    "execution": "none",
                }
                self._append(self.forecast_path, record)
                self._forecast_ids.add(forecast_id)
                added += 1
        return {"added": added, "skipped": skipped, "model_version": version}

    def score_resolved_forecasts(
        self,
        resolutions: list[dict[str, Any]],
        *,
        horizon_hours: float = 24.0,
        tolerance_hours: float = 12.0,
    ) -> dict[str, Any]:
        """Score one nearest-horizon pre-resolution forecast per market/model version."""
        if not math.isfinite(horizon_hours) or horizon_hours <= 0:
            raise ValueError("horizon_hours must be finite and positive")
        if not math.isfinite(tolerance_hours) or tolerance_hours < 0:
            raise ValueError("tolerance_hours must be finite and nonnegative")

        forecasts = _read_jsonl(_rotation_paths(self.forecast_path))
        feedback_existing = _read_jsonl(_rotation_paths(self.feedback_path))
        known_feedback = {
            str(row.get("forecast_id")) for row in feedback_existing if row.get("forecast_id")
        }
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in forecasts:
            market_id = str(row.get("market_id") or "").strip()
            version = str(row.get("model_version") or "").strip()
            stamp = _timestamp(row.get("forecast_timestamp"))
            probability = _number(row.get("model_probability_yes"))
            market_probability = _number(row.get("market_probability_yes"))
            forecast_id = str(row.get("forecast_id") or "").strip()
            if (
                not market_id or not version or stamp is None or not forecast_id
                or probability is None or not 0.0 <= probability <= 1.0
                or market_probability is None or not 0.0 <= market_probability <= 1.0
            ):
                continue
            grouped.setdefault((market_id, version), []).append({
                **row,
                "_stamp": stamp,
                "_model_probability": probability,
                "_market_probability": market_probability,
            })

        added = []
        current = datetime.now(timezone.utc).isoformat()
        tolerance_seconds = tolerance_hours * 3600.0
        target_offset = horizon_hours * 3600.0
        for resolution in resolutions or []:
            if not isinstance(resolution, dict):
                continue
            market_id = str(resolution.get("market_id") or "").strip()
            outcome = resolution.get("resolved_yes")
            resolved = _timestamp(resolution.get("resolved_at"))
            if (
                not market_id or isinstance(outcome, bool)
                or not isinstance(outcome, int) or outcome not in (0, 1)
                or resolved is None
            ):
                continue
            target = resolved - target_offset
            matching_groups = [
                (version, rows)
                for (candidate_market, version), rows in grouped.items()
                if candidate_market == market_id
            ]
            for version, rows in matching_groups:
                eligible = [
                    row for row in rows
                    if row["_stamp"] < resolved
                    and abs(row["_stamp"] - target) <= tolerance_seconds
                ]
                if not eligible:
                    continue
                chosen = min(
                    eligible,
                    key=lambda row: (abs(row["_stamp"] - target), row["_stamp"]),
                )
                forecast_id = str(chosen["forecast_id"])
                if forecast_id in known_feedback:
                    continue
                probability = float(chosen["_model_probability"])
                market_probability = float(chosen["_market_probability"])
                epsilon = 1e-12
                clipped_model = min(1.0 - epsilon, max(epsilon, probability))
                clipped_market = min(1.0 - epsilon, max(epsilon, market_probability))
                model_brier = (probability - outcome) ** 2
                market_brier = (market_probability - outcome) ** 2
                model_log_loss = -math.log(clipped_model if outcome else 1.0 - clipped_model)
                market_log_loss = -math.log(clipped_market if outcome else 1.0 - clipped_market)
                record = {
                    "forecast_id": forecast_id,
                    "model_version": version,
                    "market_id": market_id,
                    "question": str(chosen.get("question") or resolution.get("question") or "Unknown market")[:500],
                    "forecast_timestamp": str(chosen.get("forecast_timestamp") or ""),
                    "resolved_at": _iso(resolved),
                    "hours_before_resolution": round((resolved - chosen["_stamp"]) / 3600.0, 4),
                    "model_probability_yes": round(probability, 10),
                    "market_probability_yes": round(market_probability, 10),
                    "outcome_yes": outcome,
                    "model_brier_score": round(model_brier, 10),
                    "market_brier_score": round(market_brier, 10),
                    "brier_improvement_vs_market": round(market_brier - model_brier, 10),
                    "model_log_loss": round(model_log_loss, 10),
                    "market_log_loss": round(market_log_loss, 10),
                    "direction_correct": bool((probability >= 0.5) == bool(outcome)),
                    "scored_at": current,
                    "method": "nearest_forecast_to_24h_before_resolution_with_12h_tolerance",
                    "research_only": True,
                    "execution": "none",
                }
                with self._lock:
                    if forecast_id in self._feedback_ids:
                        continue
                    self._append(self.feedback_path, record)
                    self._feedback_ids.add(forecast_id)
                    known_feedback.add(forecast_id)
                added.append(record)

        return {
            "scored_added": len(added),
            "feedback_records_retained": len(feedback_existing) + len(added),
            "scoring_horizon_hours": horizon_hours,
            "tolerance_hours": tolerance_hours,
            "research_only": True,
            "execution": "none",
        }

    def record_evaluation(self, evaluation: dict[str, Any]) -> dict[str, Any]:
        """Store a compact periodic holdout/walk-forward model assessment."""
        result = evaluation if isinstance(evaluation, dict) else {}
        walk = result.get("walk_forward_validation")
        if not isinstance(walk, dict):
            walk = {}
        pooled = walk.get("pooled_out_of_sample")
        if not isinstance(pooled, dict):
            pooled = {}
        model_metrics = pooled.get("feature_model")
        market_metrics = pooled.get("market_baseline")
        if not isinstance(model_metrics, dict):
            model_metrics = {}
        if not isinstance(market_metrics, dict):
            market_metrics = {}
        record = {
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "status": str(result.get("status") or "unknown"),
            "model_version": str(result.get("model_version") or "")[:100] or None,
            "eligible_markets": int(result.get("eligible_markets") or 0),
            "train_n": int(result.get("train_n") or 0),
            "test_n": int(result.get("test_n") or 0),
            "holdout_feature_brier": _number((result.get("feature_model") or {}).get("brier_score")),
            "holdout_market_brier": _number((result.get("market_baseline") or {}).get("brier_score")),
            "walk_forward_status": str(walk.get("status") or "not_scored"),
            "walk_forward_folds_scored": int(walk.get("folds_scored") or 0),
            "walk_forward_feature_brier": _number(model_metrics.get("brier_score")),
            "walk_forward_market_brier": _number(market_metrics.get("brier_score")),
            "multi_fold_consistency_gate_passed": walk.get("multi_fold_consistency_gate_passed") is True,
            "live_execution_enabled": False,
            "research_only": True,
            "execution": "none",
        }
        with self._lock:
            self._append(self.evaluation_path, record)
        return record

    def summary(self, *, feedback_limit: int = 2000, evaluation_limit: int = 100) -> dict[str, Any]:
        """Return rolling realized-error metrics, grouped by deterministic model version."""
        feedback = _read_jsonl(_rotation_paths(self.feedback_path))[-max(1, feedback_limit):]
        evaluations = _read_jsonl(_rotation_paths(self.evaluation_path))[-max(1, evaluation_limit):]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in feedback:
            version = str(row.get("model_version") or "unknown")
            if (
                _number(row.get("model_brier_score")) is None
                or _number(row.get("market_brier_score")) is None
                or row.get("outcome_yes") not in (0, 1)
            ):
                continue
            grouped.setdefault(version, []).append(row)

        by_version = []
        for version, rows in grouped.items():
            model_brier = [float(row["model_brier_score"]) for row in rows]
            market_brier = [float(row["market_brier_score"]) for row in rows]
            model_log = [_number(row.get("model_log_loss")) for row in rows]
            valid_model_log = [value for value in model_log if value is not None]
            market_log = [_number(row.get("market_log_loss")) for row in rows]
            valid_market_log = [value for value in market_log if value is not None]
            predictions = [_number(row.get("model_probability_yes")) for row in rows]
            valid_predictions = [value for value in predictions if value is not None]
            outcomes = [int(row["outcome_yes"]) for row in rows]
            by_version.append({
                "model_version": version,
                "n": len(rows),
                "model_brier_score": round(sum(model_brier) / len(model_brier), 8),
                "market_brier_score": round(sum(market_brier) / len(market_brier), 8),
                "brier_improvement_vs_market": round(
                    sum(market_brier) / len(market_brier) - sum(model_brier) / len(model_brier), 8
                ),
                "model_log_loss": round(sum(valid_model_log) / len(valid_model_log), 8) if valid_model_log else None,
                "market_log_loss": round(sum(valid_market_log) / len(valid_market_log), 8) if valid_market_log else None,
                "mean_model_probability": round(sum(valid_predictions) / len(valid_predictions), 8) if valid_predictions else None,
                "observed_yes_rate": round(sum(outcomes) / len(outcomes), 8),
                "absolute_calibration_gap": round(
                    abs(sum(valid_predictions) / len(valid_predictions) - sum(outcomes) / len(outcomes)), 8
                ) if valid_predictions else None,
                "profitable_or_positive_pnl_claim": False,
                "interpretation": "probability forecast accuracy only; not executable trading PnL",
            })
        by_version.sort(key=lambda row: (row["n"], row["model_version"]), reverse=True)
        latest_evaluation = evaluations[-1] if evaluations else None
        return {
            "mode": "REALIZED_FORECAST_FEEDBACK_V1",
            "forecasts_scored": len(feedback),
            "model_versions_with_feedback": len(by_version),
            "performance_by_model_version": by_version[:20],
            "latest_evaluation": latest_evaluation,
            "research_only": True,
            "live_execution_enabled": False,
            "automatic_parameter_tuning": False,
            "note": (
                "Resolved outcomes are fed into future retraining and forecast errors are measured against "
                "the market baseline. Model settings are not changed automatically to avoid fitting noise."
            ),
            "execution": "none",
        }
