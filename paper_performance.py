"""Honest descriptive performance statistics for the event-aware paper ledger.

All returns are based on CLOSED simulated trades. These statistics are not an
out-of-sample validation, do not include open-position mark-to-market drawdown,
and never authorize live execution.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable

DEFAULT_MINIMUM_TRADES_FOR_EVALUATION = 60


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _aware_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _wilson_interval(wins: int, trials: int, z: float = 1.959963984540054) -> tuple[float, float] | None:
    if trials <= 0:
        return None
    proportion = wins / trials
    denominator = 1.0 + z * z / trials
    center = (proportion + z * z / (2.0 * trials)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / trials
            + z * z / (4.0 * trials * trials)
        )
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def evaluate_paper_performance(
    trades: Iterable[dict[str, Any]],
    *,
    starting_cash: float,
    minimum_trades: int = DEFAULT_MINIMUM_TRADES_FOR_EVALUATION,
) -> dict[str, Any]:
    """Summarize closed paper trades without annualizing irregular trade returns."""
    start = _finite(starting_cash)
    if start is None or start <= 0:
        raise ValueError("starting_cash must be a positive finite value")
    if isinstance(minimum_trades, bool) or not isinstance(minimum_trades, int) or minimum_trades < 1:
        raise ValueError("minimum_trades must be a positive integer")

    normalized: list[dict[str, Any]] = []
    invalid_rows = 0
    for row in trades:
        if not isinstance(row, dict):
            invalid_rows += 1
            continue
        pnl = _finite(row.get("pnl"))
        if pnl is None:
            invalid_rows += 1
            continue
        entry_fee = _finite(row.get("entry_fee"))
        exit_fee = _finite(row.get("exit_fee"))
        entry_fee = max(0.0, entry_fee or 0.0)
        exit_fee = max(0.0, exit_fee or 0.0)
        closed_at = _aware_datetime(row.get("closed_at"))
        opened_at = _aware_datetime(row.get("opened_at"))
        hold_hours = None
        if opened_at is not None and closed_at is not None:
            duration = (closed_at - opened_at).total_seconds() / 3600.0
            if duration >= 0 and math.isfinite(duration):
                hold_hours = duration
        normalized.append({
            "pnl": pnl,
            "fees": entry_fee + exit_fee,
            "hold_hours": hold_hours,
            "closed_at": closed_at,
            "trade_id": str(row.get("trade_id") or ""),
        })

    normalized.sort(
        key=lambda row: (
            row["closed_at"] or datetime.max.replace(tzinfo=timezone.utc),
            row["trade_id"],
        )
    )
    count = len(normalized)
    wins = [row for row in normalized if row["pnl"] > 1e-12]
    losses = [row for row in normalized if row["pnl"] < -1e-12]
    breakeven = count - len(wins) - len(losses)
    net_pnls = [row["pnl"] for row in normalized]
    total_net_pnl = sum(net_pnls)
    gross_profit = sum(row["pnl"] for row in wins)
    gross_loss = abs(sum(row["pnl"] for row in losses))
    total_fees = sum(row["fees"] for row in normalized)
    win_interval = _wilson_interval(len(wins), count)

    equity = start
    peak = start
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    for pnl in net_pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(0.0, peak - equity)
        drawdown_pct = drawdown / peak if peak > 0 else 0.0
        if drawdown > max_drawdown:
            max_drawdown = drawdown
        if drawdown_pct > max_drawdown_pct:
            max_drawdown_pct = drawdown_pct

    hold_values = [row["hold_hours"] for row in normalized if row["hold_hours"] is not None]
    average_hold = sum(hold_values) / len(hold_values) if hold_values else None
    expectancy = total_net_pnl / count if count else None
    average_win = gross_profit / len(wins) if wins else None
    average_loss = -gross_loss / len(losses) if losses else None
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    if count < minimum_trades:
        evaluation_status = "insufficient_sample"
    elif expectancy is not None and expectancy > 0:
        evaluation_status = "positive_in_sample_unvalidated"
    else:
        evaluation_status = "no_positive_in_sample_expectancy"

    warnings = [
        "Closed-trade statistics are descriptive and are not out-of-sample validation.",
        "Realized drawdown follows closed-trade sequence and excludes intratrade and open-position mark-to-market drawdown.",
        "No annualized Sharpe or Sortino ratio is reported because irregular trade outcomes are not a periodic return series.",
        "Visible-depth paper fills do not establish actual exchange execution quality.",
    ]
    if count < minimum_trades:
        warnings.insert(0, f"Only {count} closed trades; at least {minimum_trades} are required for the minimum evaluation sample.")
    if invalid_rows:
        warnings.append(f"{invalid_rows} invalid trade rows were excluded from the statistics.")

    return {
        "evaluation_status": evaluation_status,
        "closed_trades": count,
        "minimum_trades_required": minimum_trades,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": breakeven,
        "win_rate_pct": 100.0 * len(wins) / count if count else None,
        "win_rate_95pct_wilson_interval_pct": (
            [100.0 * win_interval[0], 100.0 * win_interval[1]] if win_interval else None
        ),
        "total_net_pnl": total_net_pnl,
        "return_on_starting_cash_pct": 100.0 * total_net_pnl / start,
        "net_expectancy_per_trade": expectancy,
        "average_win": average_win,
        "average_loss": average_loss,
        "net_profit_factor": profit_factor,
        "gross_pnl_before_fees": total_net_pnl + total_fees,
        "fees_paid": total_fees,
        "average_hold_hours": average_hold,
        "max_closed_trade_drawdown": max_drawdown,
        "max_closed_trade_drawdown_pct": 100.0 * max_drawdown_pct,
        "invalid_rows_excluded": invalid_rows,
        "out_of_sample_validated": False,
        "live_execution_eligible": False,
        "research_only": True,
        "execution": "none",
        "warnings": warnings,
    }
