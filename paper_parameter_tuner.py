"""Conservative paper-only parameter tuning with chronological holdout.

The tuner only considers stricter sentiment thresholds than the active one.
It uses already-closed paper trades, selects a candidate on the training
segment, and evaluates that single candidate once on the later holdout.
It never changes a live configuration.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _mean(rows: list[dict[str, Any]]) -> float | None:
    values = [_finite(row.get("net_roi")) for row in rows]
    usable = [value for value in values if value is not None]
    return sum(usable) / len(usable) if usable else None


def postmortem_losers(trades: list[dict[str, Any]], *, limit: int = 5) -> dict[str, Any]:
    losers = [row for row in trades if (_finite(row.get("pnl")) or 0.0) < 0]
    reasons = Counter(str(row.get("exit_reason") or "unknown") for row in losers)
    return {
        "losing_trade_count": len(losers),
        "losses_by_exit_reason": dict(reasons.most_common(limit)),
        "mean_loser_pnl": round(
            sum(_finite(row.get("pnl")) or 0.0 for row in losers) / len(losers), 8
        ) if losers else None,
    }


def tune_sentiment_threshold(
    trades: list[dict[str, Any]],
    *,
    current_threshold: float,
    minimum_closed_trades: int = 60,
    minimum_train_trades: int = 30,
    minimum_holdout_trades: int = 15,
) -> dict[str, Any]:
    """Return an auditable recommendation; apply only to a paper-only runner."""
    current = float(current_threshold)
    if not math.isfinite(current) or not 0 < current <= 1:
        raise ValueError("current_threshold must be in (0, 1]")
    cleaned = []
    for row in trades:
        if not isinstance(row, dict) or str(row.get("status") or "").upper() != "CLOSED":
            continue
        strength = _finite(row.get("signal_strength"))
        roi = _finite(row.get("net_roi"))
        pnl = _finite(row.get("pnl"))
        stamp = str(row.get("closed_at") or "")
        if strength is None or roi is None or pnl is None or not 0 <= strength <= 1 or not stamp:
            continue
        cleaned.append({**row, "signal_strength": strength, "net_roi": roi, "pnl": pnl})
    cleaned.sort(key=lambda row: str(row["closed_at"]))
    result = {
        "status": "insufficient_data",
        "applied": False,
        "current_threshold": current,
        "new_threshold": current,
        "closed_trade_count": len(cleaned),
        "train_trade_count": 0,
        "holdout_trade_count": 0,
        "baseline_holdout_mean_roi": None,
        "candidate_holdout_mean_roi": None,
        "candidate_threshold": None,
        "reason": "minimum_closed_trade_sample_not_reached",
        **postmortem_losers(cleaned),
        "research_only": True,
        "execution": "none",
    }
    if len(cleaned) < minimum_closed_trades:
        return result

    split = int(len(cleaned) * 0.70)
    train, holdout = cleaned[:split], cleaned[split:]
    baseline_holdout = [row for row in holdout if row["signal_strength"] >= current]
    baseline_mean = _mean(baseline_holdout)
    result.update({
        "train_trade_count": len(train),
        "holdout_trade_count": len(holdout),
        "baseline_holdout_mean_roi": round(baseline_mean, 8) if baseline_mean is not None else None,
    })
    if len(train) < minimum_train_trades or len(baseline_holdout) < minimum_holdout_trades:
        result["reason"] = "chronological_train_or_baseline_holdout_sample_too_small"
        return result

    candidates = [round(min(1.0, current * factor), 6) for factor in (1.0, 1.10, 1.20, 1.35, 1.50)]
    candidates = sorted(set(value for value in candidates if value > current + 1e-9))
    eligible = []
    for threshold in candidates:
        subset = [row for row in train if row["signal_strength"] >= threshold]
        if len(subset) < minimum_train_trades:
            continue
        mean_roi = _mean(subset)
        if mean_roi is not None:
            eligible.append((mean_roi, threshold, len(subset)))
    if not eligible:
        result["status"] = "no_candidate"
        result["reason"] = "no_stricter_threshold_has_enough_training_trades"
        return result

    # Selection uses training data only; the holdout is not used to rank candidates.
    _, selected_threshold, selected_train_n = max(eligible, key=lambda item: (item[0], item[1]))
    candidate_holdout = [row for row in holdout if row["signal_strength"] >= selected_threshold]
    candidate_mean = _mean(candidate_holdout)
    result.update({
        "candidate_threshold": selected_threshold,
        "candidate_train_trade_count": selected_train_n,
        "candidate_holdout_trade_count": len(candidate_holdout),
        "candidate_holdout_mean_roi": round(candidate_mean, 8) if candidate_mean is not None else None,
    })
    if len(candidate_holdout) < minimum_holdout_trades:
        result["status"] = "rejected"
        result["reason"] = "candidate_holdout_sample_too_small"
        return result

    improvement = (candidate_mean or 0.0) - (baseline_mean or 0.0)
    if candidate_mean is not None and candidate_mean > 0 and improvement >= 0.001:
        result.update({
            "status": "validated_for_paper_only",
            "applied": True,
            "new_threshold": selected_threshold,
            "reason": "candidate_improved_chronological_holdout_mean_net_roi",
            "holdout_mean_roi_improvement": round(improvement, 8),
        })
    else:
        result["status"] = "rejected"
        result["reason"] = "holdout_did_not_show_required_positive_improvement"
    return result
