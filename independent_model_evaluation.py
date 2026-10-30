"""Leakage-aware evaluation of feature-augmented binary probability models.

The model is deliberately research-only: it compares a fixed regularized
logistic model with the archived market-implied probability on a purged,
chronological holdout. It never emits orders or trade instructions.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

FEATURE_NAMES = (
    "logit_market_probability",
    "book_midpoint_minus_market",
    "book_usable",
    "depth_imbalance_top5",
    "log_spread_bps",
    "log_news_matches_24h",
    "mean_lexical_overlap",
    "log_volume_24h",
)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
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


def extract_model_features(row: dict[str, Any]) -> dict[str, float | None]:
    """Create a small, documented feature vector from one point-in-time row."""
    probability = _number(row.get("gamma_yes_probability"))
    if probability is None or not 0.0 <= probability <= 1.0:
        raise ValueError("gamma_yes_probability must be between 0 and 1")
    clipped = min(0.99, max(0.01, probability))
    logit_market = math.log(clipped / (1.0 - clipped))

    book_usable = row.get("clob_book_usable") is True
    midpoint = _number(row.get("clob_midpoint")) if book_usable else None
    spread = _number(row.get("clob_spread_bps")) if book_usable else None
    imbalance = _number(row.get("clob_depth_imbalance_top5")) if book_usable else None
    volume = _number(row.get("volume_24h"))
    news_count = _number(row.get("news_match_count_24h"))
    headlines = row.get("news_headlines")
    overlaps = []
    if isinstance(headlines, list):
        for headline in headlines:
            if isinstance(headline, dict):
                overlap = _number(headline.get("lexical_overlap"))
                if overlap is not None and 0.0 <= overlap <= 1.0:
                    overlaps.append(overlap)

    return {
        "logit_market_probability": logit_market,
        "book_midpoint_minus_market": (
            midpoint - probability if midpoint is not None and 0.0 <= midpoint <= 1.0 else None
        ),
        "book_usable": 1.0 if book_usable else 0.0,
        "depth_imbalance_top5": (
            min(1.0, max(-1.0, imbalance)) if imbalance is not None else None
        ),
        "log_spread_bps": math.log1p(spread) if spread is not None and spread >= 0 else None,
        "log_news_matches_24h": math.log1p(max(0.0, news_count or 0.0)),
        "mean_lexical_overlap": sum(overlaps) / len(overlaps) if overlaps else None,
        "log_volume_24h": math.log1p(max(0.0, volume or 0.0)),
    }


def build_feature_forecast_samples(
    feature_rows: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
    *,
    horizon_hours: float = 24.0,
    tolerance_hours: float = 12.0,
) -> list[dict[str, Any]]:
    """Select one feature snapshot per resolved market near a fixed pre-resolution horizon."""
    if horizon_hours <= 0 or tolerance_hours < 0:
        raise ValueError("horizon_hours must be positive and tolerance_hours nonnegative")
    by_market: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    for row in feature_rows:
        if not isinstance(row, dict):
            continue
        market_id = str(row.get("market_id") or "").strip()
        stamp = _timestamp(row.get("observed_at"))
        probability = _number(row.get("gamma_yes_probability"))
        if (
            market_id and stamp is not None and probability is not None
            and 0.0 <= probability <= 1.0 and row.get("research_only") is not False
        ):
            by_market.setdefault(market_id, []).append((stamp, row))
    for rows in by_market.values():
        rows.sort(key=lambda item: item[0])

    samples = []
    tolerance_seconds = tolerance_hours * 3600.0
    for resolution in resolutions:
        if not isinstance(resolution, dict):
            continue
        market_id = str(resolution.get("market_id") or "").strip()
        outcome = resolution.get("resolved_yes")
        resolved_stamp = _timestamp(resolution.get("resolved_at"))
        if (
            not market_id or outcome not in (0, 1, False, True)
            or resolved_stamp is None
        ):
            continue
        target = resolved_stamp - horizon_hours * 3600.0
        eligible = [
            (stamp, row) for stamp, row in by_market.get(market_id, [])
            if stamp < resolved_stamp and abs(stamp - target) <= tolerance_seconds
        ]
        if not eligible:
            continue
        chosen_stamp, chosen = min(
            eligible, key=lambda item: (abs(item[0] - target), item[0])
        )
        try:
            vector = extract_model_features(chosen)
        except ValueError:
            continue
        samples.append({
            "market_id": market_id,
            "question": str(resolution.get("question") or chosen.get("question") or "")[:500],
            "forecast_timestamp": _iso(chosen_stamp),
            "resolved_at": _iso(resolved_stamp),
            "horizon_hours_actual": round((resolved_stamp - chosen_stamp) / 3600.0, 3),
            "market_probability_yes": round(float(chosen["gamma_yes_probability"]), 8),
            "features": vector,
            "outcome_yes": int(bool(outcome)),
        })
    samples.sort(key=lambda row: (row["forecast_timestamp"], row["market_id"]))
    return samples


def _sigmoid(value: float) -> float:
    if value >= 35.0:
        return 1.0 - 1e-15
    if value <= -35.0:
        return 1e-15
    return 1.0 / (1.0 + math.exp(-value))


def _prepare_matrices(
    train: list[dict[str, Any]],
    test: list[dict[str, Any]],
) -> tuple[list[list[float]], list[list[float]], dict[str, Any]]:
    medians: dict[str, float] = {}
    means: dict[str, float] = {}
    scales: dict[str, float] = {}
    for name in FEATURE_NAMES:
        observed = [
            float(row["features"][name])
            for row in train
            if isinstance(row.get("features"), dict)
            and _number(row["features"].get(name)) is not None
        ]
        observed.sort()
        if observed:
            middle = len(observed) // 2
            median = observed[middle] if len(observed) % 2 else (observed[middle - 1] + observed[middle]) / 2.0
        else:
            median = 0.0
        medians[name] = median
        filled = [
            float(row["features"][name])
            if _number(row.get("features", {}).get(name)) is not None
            else median
            for row in train
        ]
        mean = sum(filled) / len(filled)
        variance = sum((value - mean) ** 2 for value in filled) / len(filled)
        scale = math.sqrt(variance)
        means[name] = mean
        scales[name] = scale if scale > 1e-9 else 1.0

    def transform(rows: list[dict[str, Any]]) -> list[list[float]]:
        matrix = []
        for row in rows:
            vector = []
            feature_values = row.get("features") if isinstance(row.get("features"), dict) else {}
            for name in FEATURE_NAMES:
                raw = _number(feature_values.get(name))
                filled = raw if raw is not None else medians[name]
                vector.append((filled - means[name]) / scales[name])
            matrix.append(vector)
        return matrix

    metadata = {
        "imputation": "training-set median only",
        "standardization": "training-set mean and standard deviation only",
        "features": list(FEATURE_NAMES),
    }
    return transform(train), transform(test), metadata


def _fit_logistic(
    matrix: list[list[float]],
    labels: list[int],
    *,
    l2: float = 0.25,
    learning_rate: float = 0.08,
    max_iterations: int = 5000,
) -> tuple[float, list[float], int]:
    """Deterministic full-batch gradient descent; intercept is not penalized."""
    if not matrix or len(matrix) != len(labels):
        raise ValueError("training matrix and labels must be non-empty and aligned")
    width = len(matrix[0])
    intercept = 0.0
    weights = [0.0] * width
    count = len(labels)
    iterations = 0
    for iteration in range(max_iterations):
        grad_intercept = 0.0
        gradients = [0.0] * width
        for features, label in zip(matrix, labels):
            probability = _sigmoid(intercept + sum(w * x for w, x in zip(weights, features)))
            error = probability - label
            grad_intercept += error
            for index, value in enumerate(features):
                gradients[index] += error * value
        grad_intercept /= count
        gradients = [gradients[i] / count + l2 * weights[i] for i in range(width)]
        intercept -= learning_rate * grad_intercept
        weights = [weights[i] - learning_rate * gradients[i] for i in range(width)]
        iterations = iteration + 1
        if max([abs(grad_intercept), *[abs(value) for value in gradients]]) < 1e-7:
            break
    return intercept, weights, iterations


def _probabilities(
    matrix: list[list[float]],
    intercept: float,
    weights: list[float],
) -> list[float]:
    return [
        min(1.0 - 1e-12, max(1e-12, _sigmoid(intercept + sum(w * x for w, x in zip(weights, row)))))
        for row in matrix
    ]


def _score(probabilities: list[float], labels: list[int]) -> dict[str, Any]:
    if not probabilities or len(probabilities) != len(labels):
        return {"n": 0, "brier_score": None, "log_loss": None, "accuracy_pct": None}
    eps = 1e-12
    n = len(labels)
    brier = sum((p - y) ** 2 for p, y in zip(probabilities, labels)) / n
    log_loss = -sum(
        y * math.log(max(eps, p)) + (1 - y) * math.log(max(eps, 1.0 - p))
        for p, y in zip(probabilities, labels)
    ) / n
    accuracy = sum((p >= 0.5) == bool(y) for p, y in zip(probabilities, labels)) / n
    return {
        "n": n,
        "brier_score": round(brier, 8),
        "log_loss": round(log_loss, 8),
        "accuracy_pct": round(accuracy * 100.0, 4),
    }


def evaluate_feature_model(
    samples: list[dict[str, Any]],
    *,
    train_fraction: float = 0.7,
    minimum_train: int = 80,
    minimum_test: int = 20,
    l2: float = 0.25,
) -> dict[str, Any]:
    """Compare a fixed logistic feature model with market prices on a purged holdout.

    Samples are split by forecast timestamp. Training rows whose outcomes were not
    resolved before the first test forecast timestamp are purged, preventing the
    model from learning labels that would not yet have been known at test time.
    """
    if not 0.5 <= train_fraction <= 0.9:
        raise ValueError("train_fraction must be between 0.5 and 0.9")
    if minimum_train < 2 or minimum_test < 1:
        raise ValueError("minimum sample sizes are too small")
    if not math.isfinite(l2) or l2 < 0:
        raise ValueError("l2 must be finite and nonnegative")

    eligible = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        forecast_stamp = _timestamp(sample.get("forecast_timestamp"))
        resolved_stamp = _timestamp(sample.get("resolved_at"))
        outcome = sample.get("outcome_yes")
        market_probability = _number(sample.get("market_probability_yes"))
        features = sample.get("features")
        if (
            forecast_stamp is None or resolved_stamp is None or forecast_stamp >= resolved_stamp
            or outcome not in (0, 1, False, True)
            or market_probability is None or not 0.0 <= market_probability <= 1.0
            or not isinstance(features, dict)
        ):
            continue
        try:
            vector = extract_model_features({
                "gamma_yes_probability": market_probability,
                **features,
            })
        except ValueError:
            continue
        eligible.append({
            **sample,
            "forecast_timestamp": _iso(forecast_stamp),
            "resolved_at": _iso(resolved_stamp),
            "features": vector,
            "outcome_yes": int(bool(outcome)),
            "_forecast_stamp": forecast_stamp,
            "_resolved_stamp": resolved_stamp,
        })
    eligible.sort(key=lambda row: (row["_forecast_stamp"], row["market_id"] if row.get("market_id") else ""))

    split_index = int(len(eligible) * train_fraction)
    initial_train = eligible[:split_index]
    test = eligible[split_index:]
    test_start = test[0]["_forecast_stamp"] if test else None
    train = [
        row for row in initial_train
        if test_start is not None and row["_resolved_stamp"] < test_start
    ]
    train.sort(key=lambda row: row["_forecast_stamp"])

    result = {
        "mode": "INDEPENDENT_FEATURE_MODEL_EVALUATION_V1",
        "status": "insufficient_data",
        "eligible_markets": len(eligible),
        "initial_train_n": len(initial_train),
        "purged_train_n": len(initial_train) - len(train),
        "train_n": len(train),
        "test_n": len(test),
        "train_fraction": train_fraction,
        "test_forecast_start": _iso(test_start) if test_start is not None else None,
        "latest_training_label_available_at": (
            _iso(max(row["_resolved_stamp"] for row in train)) if train else None
        ),
        "minimum_train": minimum_train,
        "minimum_test": minimum_test,
        "research_only": True,
        "forecast": None,
        "execution": "none",
    }
    if len(train) < minimum_train or len(test) < minimum_test:
        return {
            **result,
            "warning": (
                "Not enough leakage-safe, resolved feature samples. Keep collecting "
                "point-in-time snapshots; the evaluator will not fit a model early."
            ),
        }
    if len({row["outcome_yes"] for row in train}) < 2:
        return {
            **result,
            "status": "insufficient_class_diversity",
            "warning": "Training window contains only one outcome class; model fitting was withheld.",
        }

    latest_train_resolution = max(row["_resolved_stamp"] for row in train)
    if latest_train_resolution >= test_start:
        raise RuntimeError("purge invariant violated: a training label was not known at test start")

    train_labels = [row["outcome_yes"] for row in train]
    test_labels = [row["outcome_yes"] for row in test]
    train_matrix, test_matrix, transform_metadata = _prepare_matrices(train, test)
    intercept, weights, iterations = _fit_logistic(
        train_matrix, train_labels, l2=l2
    )
    model_probabilities = _probabilities(test_matrix, intercept, weights)
    market_probabilities = [row["market_probability_yes"] for row in test]
    model_metrics = _score(model_probabilities, test_labels)
    market_metrics = _score(market_probabilities, test_labels)
    brier_improvement = market_metrics["brier_score"] - model_metrics["brier_score"]
    brier_skill = (
        brier_improvement / market_metrics["brier_score"]
        if market_metrics["brier_score"] > 0 else None
    )
    log_loss_change = model_metrics["log_loss"] - market_metrics["log_loss"]

    return {
        **result,
        "status": "scored",
        "market_baseline": market_metrics,
        "feature_model": model_metrics,
        "brier_improvement_vs_market": round(brier_improvement, 8),
        "brier_skill_vs_market": round(brier_skill, 8) if brier_skill is not None else None,
        "log_loss_change_vs_market": round(log_loss_change, 8),
        "model": {
            "type": "regularized_logistic_regression",
            "l2": l2,
            "iterations": iterations,
            **transform_metadata,
        },
        "test_outcome_yes_rate": round(sum(test_labels) / len(test_labels), 8),
        "label_availability_check": "all training outcomes resolved before first test forecast",
        "warning": (
            "Single chronological holdout only; no statistical significance or "
            "future-performance claim. A positive score difference may be noise."
        ),
    }


def _load_jsonl(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for path in paths:
        candidate = Path(path)
        if not candidate.exists():
            continue
        try:
            with candidate.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(value, dict):
                        output.append(value)
        except OSError:
            continue
    return output


def evaluate_feature_files(
    feature_paths: Iterable[str | Path],
    resolution_paths: Iterable[str | Path],
    *,
    horizon_hours: float = 24.0,
    tolerance_hours: float = 12.0,
) -> dict[str, Any]:
    feature_rows = _load_jsonl(feature_paths)
    resolutions = _load_jsonl(resolution_paths)
    samples = build_feature_forecast_samples(
        feature_rows,
        resolutions,
        horizon_hours=horizon_hours,
        tolerance_hours=tolerance_hours,
    )
    evaluation = evaluate_feature_model(samples)
    return {
        **evaluation,
        "horizon_hours": horizon_hours,
        "tolerance_hours": tolerance_hours,
        "feature_rows_loaded": len(feature_rows),
        "resolutions_loaded": len(resolutions),
        "samples_selected": len(samples),
        "data_requirements": {
            "one_near_horizon_snapshot_per_resolved_binary_market": True,
            "features_timestamped_before_resolution": True,
            "training_labels_known_before_test_forecasts": True,
            "minimum_training_markets": 80,
            "minimum_test_markets": 20,
        },
        "limitations": [
            "This evaluates prediction scores, not fills, fees, slippage, or P&L.",
            "The single holdout is exploratory and can be noisy or regime-specific.",
            "News inputs are lexical matches and do not establish event truth or source independence.",
            "Only the latest retained JSONL rotations are available; missing historical snapshots cannot be reconstructed.",
            "Model features and preprocessing are fixed; this is not an automatically optimized trading strategy.",
        ],
    }
