"""Volatility-aware risk sizing and exit levels for offline paper simulations."""
from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from typing import Iterable


@dataclass(frozen=True)
class DynamicExitLevels:
    stop_loss_price: float
    take_profit_price: float
    stop_distance: float
    take_distance: float
    observed_volatility: float
    method: str = "rolling_absolute_price_change_volatility_v1"

    def as_dict(self) -> dict:
        return asdict(self)


def dynamic_exit_levels(
    *,
    entry_price: float,
    price_history: Iterable[float],
    volatility_multiplier: float = 2.0,
    reward_to_risk: float = 2.0,
    fallback_volatility: float = 0.02,
    minimum_stop_distance: float = 0.02,
    maximum_stop_distance: float = 0.15,
) -> DynamicExitLevels:
    """Compute bounded long-outcome stop and target from recent price changes."""
    values = [float(x) for x in price_history if _valid_price(x)]
    entry = float(entry_price)
    if not _valid_price(entry):
        raise ValueError("entry_price must be strictly between zero and one")
    if (
        not math.isfinite(volatility_multiplier) or volatility_multiplier <= 0
        or not math.isfinite(reward_to_risk) or reward_to_risk <= 0
        or not math.isfinite(fallback_volatility) or fallback_volatility <= 0
        or not math.isfinite(minimum_stop_distance) or minimum_stop_distance <= 0
        or not math.isfinite(maximum_stop_distance)
        or maximum_stop_distance < minimum_stop_distance
    ):
        raise ValueError("invalid dynamic exit policy")

    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    volatility = statistics.stdev(changes) if len(changes) >= 2 else fallback_volatility
    if not math.isfinite(volatility) or volatility <= 0:
        volatility = fallback_volatility
    raw_distance = volatility * volatility_multiplier
    distance = min(maximum_stop_distance, max(minimum_stop_distance, raw_distance))
    distance = min(distance, max(0.005, entry - 0.005))
    stop = max(0.005, entry - distance)
    actual_stop_distance = entry - stop
    target = min(0.995, entry + actual_stop_distance * reward_to_risk)
    return DynamicExitLevels(
        stop_loss_price=round(stop, 8),
        take_profit_price=round(target, 8),
        stop_distance=round(actual_stop_distance, 8),
        take_distance=round(target - entry, 8),
        observed_volatility=round(volatility, 8),
    )


def size_position_by_risk(
    *,
    equity: float,
    available_cash: float,
    remaining_exposure_capacity: float,
    entry_price: float,
    stop_distance: float,
    risk_per_trade_pct: float = 0.01,
    max_position_usd: float = 5.0,
) -> dict:
    """Risk-budget sizing: notional * (stop distance / entry) <= risk budget."""
    values = (equity, available_cash, remaining_exposure_capacity, entry_price,
              stop_distance, risk_per_trade_pct, max_position_usd)
    if any(not math.isfinite(float(v)) for v in values):
        raise ValueError("risk sizing inputs must be finite")
    if (
        equity <= 0 or available_cash < 0 or remaining_exposure_capacity < 0
        or not 0 < entry_price < 1 or stop_distance <= 0
        or not 0 < risk_per_trade_pct <= 0.05 or max_position_usd <= 0
    ):
        raise ValueError("risk sizing inputs are outside safe bounds")
    risk_budget = equity * risk_per_trade_pct
    risk_limited_notional = risk_budget * entry_price / stop_distance
    notional = min(
        max_position_usd, available_cash, remaining_exposure_capacity,
        risk_limited_notional,
    )
    shares = notional / entry_price if notional > 0 else 0.0
    expected_stop_loss = shares * stop_distance
    return {
        "approved": notional > 0 and shares > 0,
        "notional_usd": round(notional, 8),
        "shares": round(shares, 10),
        "risk_budget_usd": round(risk_budget, 8),
        "estimated_loss_at_stop_usd": round(expected_stop_loss, 8),
        "risk_per_trade_pct": risk_per_trade_pct,
        "sizing_method": "fractional_equity_risk_budget",
        "execution": "none",
    }


def daily_drawdown_breached(
    *,
    day_start_equity: float,
    current_equity: float,
    max_daily_drawdown_pct: float,
) -> bool:
    values = (day_start_equity, current_equity, max_daily_drawdown_pct)
    if any(not math.isfinite(float(v)) for v in values):
        raise ValueError("drawdown inputs must be finite")
    if day_start_equity <= 0 or not 0 < max_daily_drawdown_pct <= 0.25:
        raise ValueError("invalid daily drawdown policy")
    return current_equity <= day_start_equity * (1.0 - max_daily_drawdown_pct)


def _valid_price(value: float) -> bool:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(parsed) and 0.0 < parsed < 1.0
