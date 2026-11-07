"""Conservative paper-only parameter tuning with multi-window chronological validation.

The tuner selects stricter thresholds using only an earlier training segment.
It evaluates that single selected threshold on three later, contiguous
chronological holdout windows. A paper threshold is changed only when every
window shows positive net ROI and a minimum improvement over the active policy.
It never changes a live configuration.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone
from typing import Any


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _closed_timestamp(value: Any) -> tuple[float, str] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    parsed = parsed.astimezone(timezone.utc)
    return parsed.timestamp(), parsed.isoformat()


def _mean(rows: list[dict[str, Any]]) -> float | None:
    values = [_finite(row.get("net_roi")) for row in rows]
    usable = [value for value in values if value is not None]
    return sum(usable) / len(usable) if usable else None


def _sum_pnl(rows: list[dict[str, Any]]) -> float:
    return sum(_finite(row.get("pnl")) or 0.0 for row in rows)


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
    holdout_folds: int = 3,
    minimum_fold_trades: int = 5,
) -> dict[str, Any]:
    """Return an auditable paper-only recommendation; require all later folds to pass."""
    current = float(current_threshold)
    if not math.isfinite(current) or not 0 < current <= 1:
        raise ValueError("current_threshold must be in (0, 1]")
    if minimum_closed_trades < 1 or minimum_train_trades < 2 or minimum_holdout_trades < 1:
        raise ValueError("minimum sample sizes must be positive and training must require at least two trades")
    if not 2 <= holdout_folds <= 5:
        raise ValueError("holdout_folds must be between 2 and 5")
    if minimum_fold_trades < 2:
        raise ValueError("minimum_fold_trades must be at least two")

    source_rows = [
        row for row in trades
        if isinstance(row, dict) and str(row.get("status") or "").upper() == "CLOSED"
    ]
    trade_id_counts = Counter(
        str(row.get("trade_id")).strip()
        for row in source_rows
        if row.get("trade_id") is not None and str(row.get("trade_id")).strip()
    )
    duplicate_trade_ids = {
        trade_id for trade_id, count in trade_id_counts.items() if count > 1
    }
    market_id_counts = Counter(
        str(row.get("market_id")).strip()
        for row in source_rows
        if row.get("market_id") is not None and str(row.get("market_id")).strip()
    )
    duplicate_market_ids = {
        market_id for market_id, count in market_id_counts.items() if count > 1
    }

    cleaned = []
    invalid_timestamp_count = 0
    invalid_metric_count = 0
    for row in source_rows:
        trade_id = str(row.get("trade_id")).strip() if row.get("trade_id") is not None else ""
        market_id = str(row.get("market_id")).strip() if row.get("market_id") is not None else ""
        if (trade_id and trade_id in duplicate_trade_ids) or (market_id and market_id in duplicate_market_ids):
            continue
        strength = _finite(row.get("signal_strength"))
        roi = _finite(row.get("net_roi"))
        pnl = _finite(row.get("pnl"))
        parsed_stamp = _closed_timestamp(row.get("closed_at"))
        if parsed_stamp is None:
            invalid_timestamp_count += 1
            continue
        if strength is None or roi is None or pnl is None or not 0 <= strength <= 1:
            invalid_metric_count += 1
            continue
        stamp_epoch, stamp_iso = parsed_stamp
        cleaned.append({
            **row,
            "closed_at": stamp_iso,
            "signal_strength": strength,
            "net_roi": roi,
            "pnl": pnl,
            "_closed_epoch": stamp_epoch,
        })
    cleaned.sort(key=lambda row: (row["_closed_epoch"], str(row.get("trade_id") or "")))

    result = {
        "status": "insufficient_data",
        "applied": False,
        "current_threshold": current,
        "new_threshold": current,
        "closed_trade_count": len(cleaned),
        "invalid_timestamp_count": invalid_timestamp_count,
        "invalid_metric_count": invalid_metric_count,
        "duplicate_trade_ids_excluded": len(duplicate_trade_ids),
        "duplicate_market_ids_excluded": len(duplicate_market_ids),
        "train_trade_count": 0,
        "holdout_trade_count": 0,
        "holdout_folds_requested": holdout_folds,
        "folds_scored": 0,
        "fold_reports": [],
        "baseline_holdout_mean_roi": None,
        "candidate_holdout_mean_roi": None,
        "candidate_threshold": None,
        "holdout_mean_roi_improvement": None,
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
    baseline_holdout_net_pnl = _sum_pnl(baseline_holdout)
    result.update({
        "train_trade_count": len(train),
        "holdout_trade_count": len(holdout),
        "baseline_holdout_trade_count": len(baseline_holdout),
        "baseline_holdout_mean_roi": round(baseline_mean, 8) if baseline_mean is not None else None,
        "baseline_holdout_net_pnl": round(baseline_holdout_net_pnl, 8),
    })
    if (
        len(train) < minimum_train_trades
        or len(holdout) < minimum_holdout_trades
        or len(baseline_holdout) < minimum_holdout_trades
    ):
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

    # Candidate ranking is strictly limited to the earlier training segment.
    _, selected_threshold, selected_train_n = max(eligible, key=lambda item: (item[0], item[1]))
    candidate_holdout = [row for row in holdout if row["signal_strength"] >= selected_threshold]
    candidate_mean = _mean(candidate_holdout)
    result.update({
        "candidate_threshold": selected_threshold,
        "candidate_train_trade_count": selected_train_n,
        "candidate_holdout_trade_count": len(candidate_holdout),
        "candidate_holdout_mean_roi": round(candidate_mean, 8) if candidate_mean is not None else None,
        "candidate_holdout_net_pnl": round(_sum_pnl(candidate_holdout), 8),
        "holdout_net_pnl_improvement": round(
            _sum_pnl(candidate_holdout) - baseline_holdout_net_pnl, 8
        ),
    })
    if len(candidate_holdout) < minimum_holdout_trades:
        result["status"] = "rejected"
        result["reason"] = "candidate_holdout_sample_too_small"
        return result

    fold_reports = []
    fold_passes = []
    for fold_index in range(holdout_folds):
        start = len(holdout) * fold_index // holdout_folds
        end = len(holdout) * (fold_index + 1) // holdout_folds
        fold = holdout[start:end]
        baseline_fold = [row for row in fold if row["signal_strength"] >= current]
        candidate_fold = [row for row in fold if row["signal_strength"] >= selected_threshold]
        fold_report = {
            "fold": fold_index + 1,
            "test_trade_count": len(fold),
            "baseline_trade_count": len(baseline_fold),
            "candidate_trade_count": len(candidate_fold),
            "test_start": fold[0]["closed_at"] if fold else None,
            "test_end": fold[-1]["closed_at"] if fold else None,
            "status": "insufficient_data",
            "baseline_mean_net_roi": None,
            "candidate_mean_net_roi": None,
            "baseline_net_pnl": round(_sum_pnl(baseline_fold), 8),
            "candidate_net_pnl": round(_sum_pnl(candidate_fold), 8),
            "net_pnl_improvement": round(_sum_pnl(candidate_fold) - _sum_pnl(baseline_fold), 8),
            "net_roi_improvement": None,
        }
        if (
            len(baseline_fold) < minimum_fold_trades
            or len(candidate_fold) < minimum_fold_trades
        ):
            fold_report["reason"] = "baseline_or_candidate_fold_sample_too_small"
            fold_passes.append(False)
            fold_reports.append(fold_report)
            continue

        baseline_fold_mean = _mean(baseline_fold)
        candidate_fold_mean = _mean(candidate_fold)
        improvement = (
            candidate_fold_mean - baseline_fold_mean
            if candidate_fold_mean is not None and baseline_fold_mean is not None
            else None
        )
        passed = (
            candidate_fold_mean is not None
            and candidate_fold_mean > 0
            and _sum_pnl(candidate_fold) > 0
            and _sum_pnl(candidate_fold) >= _sum_pnl(baseline_fold)
            and improvement is not None
            and improvement >= 0.001
        )
        fold_report.update({
            "status": "passed" if passed else "rejected",
            "baseline_mean_net_roi": round(baseline_fold_mean, 8) if baseline_fold_mean is not None else None,
            "candidate_mean_net_roi": round(candidate_fold_mean, 8) if candidate_fold_mean is not None else None,
            "net_roi_improvement": round(improvement, 8) if improvement is not None else None,
            "reason": (
                "positive_net_roi_improvement_and_non_decreasing_total_net_pnl"
                if passed else
                "fold_did_not_meet_positive_net_roi_improvement_and_net_pnl"
            ),
        })
        fold_passes.append(passed)
        fold_reports.append(fold_report)

    result["fold_reports"] = fold_reports
    result["folds_scored"] = sum(1 for row in fold_reports if row["status"] in {"passed", "rejected"})
    result["baseline_holdout_mean_roi"] = round(baseline_mean, 8) if baseline_mean is not None else None
    pooled_improvement = (
        candidate_mean - baseline_mean
        if candidate_mean is not None and baseline_mean is not None
        else None
    )
    result["holdout_mean_roi_improvement"] = (
        round(pooled_improvement, 8) if pooled_improvement is not None else None
    )

    if len(fold_passes) != holdout_folds or not all(fold_passes):
        result["status"] = "rejected"
        result["reason"] = "candidate_failed_one_or_more_chronological_holdout_folds"
        return result

    if (
        candidate_mean is not None
        and candidate_mean > 0
        and _sum_pnl(candidate_holdout) > 0
        and _sum_pnl(candidate_holdout) >= baseline_holdout_net_pnl
        and pooled_improvement is not None
        and pooled_improvement >= 0.001
    ):
        result.update({
            "status": "validated_for_paper_only",
            "applied": True,
            "new_threshold": selected_threshold,
            "reason": "candidate_improved_every_fold_without_reducing_total_net_pnl",
        })
    else:
        result["status"] = "rejected"
        result["reason"] = "holdout_did_not_show_required_pooled_roi_and_net_pnl_improvement"
    return result
