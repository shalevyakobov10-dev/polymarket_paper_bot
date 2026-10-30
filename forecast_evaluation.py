"""Leakage-aware binary forecast evaluation for resolved Polymarket markets.

This module scores archived public market-implied probabilities. It does not
create betting instructions, place orders, or claim predictive edge.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _list_field(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def normalize_binary_resolution(market: dict[str, Any]) -> dict[str, Any] | None:
    """Return a resolved Yes/No outcome only when Gamma prices are decisive."""
    if not isinstance(market, dict) or not _truthy(market.get("closed")):
        return None
    outcomes = _list_field(market.get("outcomes"))
    prices = _list_field(market.get("outcomePrices"))
    tokens = _list_field(market.get("clobTokenIds"))
    if len(outcomes) != 2 or len(prices) != 2:
        return None
    normalized = [str(item).strip().casefold() for item in outcomes]
    if "yes" not in normalized or "no" not in normalized:
        return None
    yes_index = normalized.index("yes")
    no_index = normalized.index("no")
    yes_price = _number(prices[yes_index])
    no_price = _number(prices[no_index])
    if yes_price is None or no_price is None:
        return None
    if not (0.99 <= yes_price <= 1.0 and 0.0 <= no_price <= 0.01):
        if not (0.99 <= no_price <= 1.0 and 0.0 <= yes_price <= 0.01):
            return None

    market_id = str(market.get("conditionId") or market.get("id") or "").strip()
    if not market_id:
        return None
    resolved_at_raw = market.get("closedTime") or market.get("endDate") or market.get("endDateIso")
    resolved_at = _timestamp(resolved_at_raw)
    if resolved_at is None:
        return None
    yes_token_id = str(tokens[yes_index]).strip() if yes_index < len(tokens) else ""
    return {
        "market_id": market_id,
        "gamma_market_id": str(market.get("id") or ""),
        "slug": str(market.get("slug") or ""),
        "question": str(market.get("question") or "Unknown market")[:500],
        "yes_token_id": yes_token_id,
        "resolved_yes": 1 if yes_price >= 0.99 else 0,
        "resolved_at": datetime.fromtimestamp(resolved_at, tz=timezone.utc).isoformat(),
        "resolved_at_source": "closedTime" if market.get("closedTime") else "endDate",
        "resolution_source": str(market.get("resolutionSource") or ""),
        "source": "public_gamma_resolved_market",
        "research_only": True,
        "execution": "none",
    }


def build_binary_forecast_samples(
    snapshots: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
    *,
    horizon_hours: float = 24.0,
    tolerance_hours: float = 12.0,
) -> list[dict[str, Any]]:
    """Choose one pre-resolution Yes-price observation near a fixed horizon.

    The chosen observation must be before resolution and within the tolerance
    band around the target horizon. No post-resolution or post-target data is
    allowed to enter the sample.
    """
    if horizon_hours <= 0 or tolerance_hours < 0:
        raise ValueError("horizon_hours must be positive and tolerance_hours nonnegative")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in snapshots:
        if not isinstance(row, dict):
            continue
        market_id = str(row.get("market_id") or "").strip()
        price = _number(row.get("observed_price"))
        stamp = _timestamp(row.get("timestamp"))
        outcome = str(row.get("outcome") or "").strip().casefold()
        if market_id and price is not None and 0.0 <= price <= 1.0 and stamp is not None and outcome == "yes":
            grouped[market_id].append({**row, "_stamp": stamp, "_price": price})
    for rows in grouped.values():
        rows.sort(key=lambda item: item["_stamp"])

    samples = []
    for resolution in resolutions:
        if not isinstance(resolution, dict):
            continue
        market_id = str(resolution.get("market_id") or "").strip()
        resolved_yes = resolution.get("resolved_yes")
        resolved_stamp = _timestamp(resolution.get("resolved_at"))
        if not market_id or resolved_yes not in (0, 1, False, True) or resolved_stamp is None:
            continue
        candidates = grouped.get(market_id, [])
        if not candidates:
            continue
        target_stamp = resolved_stamp - horizon_hours * 3600.0
        tolerance_seconds = tolerance_hours * 3600.0
        eligible = [
            row for row in candidates
            if row["_stamp"] < resolved_stamp
            and abs(row["_stamp"] - target_stamp) <= tolerance_seconds
        ]
        if not eligible:
            continue
        chosen = min(eligible, key=lambda row: (abs(row["_stamp"] - target_stamp), row["_stamp"]))
        sample_stamp = float(chosen["_stamp"])
        samples.append({
            "market_id": market_id,
            "question": str(resolution.get("question") or chosen.get("question") or "Unknown market")[:500],
            "forecast_timestamp": datetime.fromtimestamp(sample_stamp, tz=timezone.utc).isoformat(),
            "resolved_at": resolution["resolved_at"],
            "horizon_hours_actual": round((resolved_stamp - sample_stamp) / 3600.0, 3),
            "predicted_probability_yes": round(float(chosen["_price"]), 8),
            "outcome_yes": int(bool(resolved_yes)),
            "token_id": str(chosen.get("token_id") or ""),
            "method": "nearest_pre_resolution_yes_price_at_fixed_horizon",
            "research_only": True,
        })
    return samples


def score_binary_forecasts(
    samples: list[dict[str, Any]],
    *,
    calibration_bins: int = 10,
) -> dict[str, Any]:
    """Calculate Brier, log loss, Brier skill, accuracy and calibration."""
    if calibration_bins < 2 or calibration_bins > 50:
        raise ValueError("calibration_bins must be between 2 and 50")
    cleaned = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        probability = _number(sample.get("predicted_probability_yes"))
        outcome = sample.get("outcome_yes")
        if probability is None or not 0.0 <= probability <= 1.0 or outcome not in (0, 1, False, True):
            continue
        cleaned.append((probability, int(bool(outcome))))

    count = len(cleaned)
    base = {
        "n": count,
        "method": "binary probability scoring on one pre-resolution Yes-price sample per market",
        "research_only": True,
        "execution": "none",
        "calibration_bins": [],
    }
    if not cleaned:
        return {
            **base,
            "brier_score": None,
            "log_loss": None,
            "accuracy_pct": None,
            "base_rate_yes": None,
            "brier_skill_vs_sample_base_rate": None,
            "expected_calibration_error": None,
            "warning": "No eligible resolved forecast samples; collect more pre-resolution snapshots.",
        }

    eps = 1e-15
    brier = sum((p - y) ** 2 for p, y in cleaned) / count
    logloss = -sum(
        y * math.log(max(eps, p)) + (1 - y) * math.log(max(eps, 1.0 - p))
        for p, y in cleaned
    ) / count
    base_rate = sum(y for _, y in cleaned) / count
    baseline_brier = sum((base_rate - y) ** 2 for _, y in cleaned) / count
    brier_skill = 1.0 - brier / baseline_brier if baseline_brier > 0 else None
    accuracy = sum((p >= 0.5) == bool(y) for p, y in cleaned) / count

    bin_rows = []
    weighted_calibration_error = 0.0
    for index in range(calibration_bins):
        lower = index / calibration_bins
        upper = (index + 1) / calibration_bins
        members = [
            (p, y) for p, y in cleaned
            if lower <= p < upper or (index == calibration_bins - 1 and p == 1.0)
        ]
        if not members:
            continue
        mean_probability = sum(p for p, _ in members) / len(members)
        observed_frequency = sum(y for _, y in members) / len(members)
        gap = abs(mean_probability - observed_frequency)
        weighted_calibration_error += len(members) / count * gap
        bin_rows.append({
            "lower_bound": round(lower, 4),
            "upper_bound": round(upper, 4),
            "n": len(members),
            "mean_predicted_probability": round(mean_probability, 6),
            "observed_yes_frequency": round(observed_frequency, 6),
            "absolute_calibration_gap": round(gap, 6),
        })

    return {
        **base,
        "brier_score": round(brier, 8),
        "log_loss": round(logloss, 8),
        "accuracy_pct": round(accuracy * 100.0, 4),
        "base_rate_yes": round(base_rate, 8),
        "brier_skill_vs_sample_base_rate": round(brier_skill, 8) if brier_skill is not None else None,
        "expected_calibration_error": round(weighted_calibration_error, 8),
        "calibration_bins": bin_rows,
        "warning": (
            "Very small evaluation sample; metrics are highly uncertain."
            if count < 30 else
            "Exploratory evaluation only; require more markets and later chronological holdouts."
            if count < 100 else
            "Historical diagnostics only; not evidence of future profitability or a validated strategy."
        ),
    }


def load_jsonl(paths: list[str | Path]) -> list[dict[str, Any]]:
    """Load valid JSON objects from one or more append-only JSONL files."""
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


def evaluate_jsonl_files(
    snapshot_paths: list[str | Path],
    resolution_paths: list[str | Path],
    *,
    horizon_hours: float = 24.0,
    tolerance_hours: float = 12.0,
) -> dict[str, Any]:
    snapshots = load_jsonl(snapshot_paths)
    resolutions = load_jsonl(resolution_paths)
    samples = build_binary_forecast_samples(
        snapshots, resolutions,
        horizon_hours=horizon_hours,
        tolerance_hours=tolerance_hours,
    )
    scores = score_binary_forecasts(samples)
    return {
        "mode": "RESOLVED_MARKET_FORECAST_EVALUATION",
        "horizon_hours": horizon_hours,
        "tolerance_hours": tolerance_hours,
        "snapshots_loaded": len(snapshots),
        "resolutions_loaded": len(resolutions),
        "eligible_samples": len(samples),
        "metrics": scores,
        "sample_preview": samples[-10:],
        "limitations": [
            "Only binary Yes/No markets with decisive resolved outcomes are currently scored.",
            "The forecast is the archived Polymarket Yes outcome price, not an independent model estimate.",
            "One observation is selected near the configured pre-resolution horizon; markets without one are excluded.",
            "This scorecard evaluates market-implied probabilities, not a trading strategy, executable fills, or profitability.",
            "Small samples and changing market populations can produce unstable estimates.",
        ],
    }
