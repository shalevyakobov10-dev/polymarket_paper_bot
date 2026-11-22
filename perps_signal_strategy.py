"""Conservative, paper-only trend signal for Polymarket Perps.

Signals are computed from completed one-minute mark-price bars in the local
snapshot archive. This is an experimental EMA/momentum research baseline, not
a calibrated forecast or evidence of a profitable trading edge. It has no
network, account, wallet, signing, or order-submission capability.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


ZERO = Decimal("0")
ONE = Decimal("1")
MIN_STOP = Decimal("0.005")
MAX_STOP = Decimal("0.015")


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"invalid_{field}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"invalid_{field}") from None
    if not result.is_finite():
        raise ValueError(f"invalid_{field}")
    return result


def _utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid_snapshot_timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("naive_snapshot_timestamp")
    return parsed.astimezone(timezone.utc)


def _ema(values: Sequence[Decimal], period: int) -> Decimal:
    if period < 1 or len(values) < period:
        raise ValueError("insufficient_ema_history")
    alpha = Decimal("2") / Decimal(period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (ONE - alpha) * result
    return result


@dataclass(frozen=True)
class PerpsStrategyConfig:
    fast_ema_period: int = 8
    slow_ema_period: int = 21
    minimum_completed_bars: int = 60
    momentum_bars: int = 5
    volatility_lookback: int = 14
    minimum_abs_volatility_pct: Decimal = Decimal("0.0002")
    maximum_abs_volatility_pct: Decimal = Decimal("0.01")
    volatility_stop_multiple: Decimal = Decimal("3")
    minimum_stop_distance_pct: Decimal = MIN_STOP
    maximum_stop_distance_pct: Decimal = MAX_STOP
    reward_risk_multiple: Decimal = Decimal("2")
    entry_notional_usd: Decimal = Decimal("10")
    max_signal_age_seconds: int = 120

    def __post_init__(self) -> None:
        integer_bounds = {
            "fast_ema_period": (2, 50),
            "slow_ema_period": (3, 200),
            "minimum_completed_bars": (60, 500),
            "momentum_bars": (2, 30),
            "volatility_lookback": (2, 100),
            "max_signal_age_seconds": (30, 300),
        }
        for name, (minimum, maximum) in integer_bounds.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if self.fast_ema_period >= self.slow_ema_period:
            raise ValueError("fast_ema_period_must_be_less_than_slow_ema_period")
        if self.minimum_completed_bars < max(
            self.slow_ema_period + 2, self.momentum_bars + 2, self.volatility_lookback + 2
        ):
            raise ValueError("minimum_completed_bars_too_small")
        positive = (
            "minimum_abs_volatility_pct", "maximum_abs_volatility_pct",
            "volatility_stop_multiple", "minimum_stop_distance_pct",
            "maximum_stop_distance_pct", "reward_risk_multiple", "entry_notional_usd",
        )
        for name in positive:
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
                raise ValueError(f"{name} must be a positive Decimal")
        if (
            self.maximum_abs_volatility_pct < self.minimum_abs_volatility_pct
            or self.maximum_abs_volatility_pct > Decimal("0.05")
        ):
            raise ValueError("invalid_volatility_band")
        if self.volatility_stop_multiple > Decimal("10"):
            raise ValueError("volatility_stop_multiple_too_large")
        if self.minimum_stop_distance_pct < MIN_STOP or self.maximum_stop_distance_pct > MAX_STOP:
            raise ValueError("stop_distance_must_remain_within_0.5_to_1.5_percent")
        if self.maximum_stop_distance_pct < self.minimum_stop_distance_pct:
            raise ValueError("invalid_stop_distance_range")
        if not Decimal("1.5") <= self.reward_risk_multiple <= Decimal("3"):
            raise ValueError("reward_risk_multiple_must_be_between_1_5_and_3")
        if not Decimal("1") <= self.entry_notional_usd <= Decimal("10"):
            raise ValueError("entry_notional_must_be_between_1_and_10_usd")


class PerpsEmaMomentumStrategy:
    """One-minute EMA crossover with momentum/volatility filters and no lookahead."""

    def __init__(self, config: PerpsStrategyConfig | None = None) -> None:
        self.config = config or PerpsStrategyConfig()

    def completed_minute_bars(
        self,
        snapshots: Sequence[Mapping[str, Any]],
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now_must_be_timezone_aware")
        now_ms = int(current.astimezone(timezone.utc).timestamp() * 1000)
        buckets: dict[int, tuple[datetime, Decimal]] = {}
        for snapshot in snapshots:
            if not isinstance(snapshot, Mapping):
                continue
            try:
                observed = _utc(snapshot.get("observed_at"))
                mark = _decimal(snapshot.get("mark_price"), "mark_price")
                if mark <= ZERO:
                    continue
                timestamp_ms = int(observed.timestamp() * 1000)
                if timestamp_ms > now_ms:
                    continue
                bucket = timestamp_ms // 60_000
                bucket_end_ms = (bucket + 1) * 60_000
                # Do not use an incomplete candle.
                if bucket_end_ms > now_ms:
                    continue
                previous = buckets.get(bucket)
                if previous is None or observed > previous[0]:
                    buckets[bucket] = (observed, mark)
            except (ValueError, TypeError, OverflowError):
                continue
        bars = [
            {"bucket": bucket, "close_ms": (bucket + 1) * 60_000, "close": item[1]}
            for bucket, item in sorted(buckets.items())
        ]
        return bars

    def evaluate(
        self,
        snapshots: Sequence[Mapping[str, Any]],
        *,
        current_snapshot: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        bars = self.completed_minute_bars(snapshots, now=current)
        config = self.config
        base = {
            "strategy": "ema_8_21_momentum_volatility_v1",
            "research_only": True,
            "execution": "none",
            "live_execution_eligible": False,
            "signal": False,
            "side": None,
            "bar_close_ms": bars[-1]["close_ms"] if bars else None,
            "completed_bars": len(bars),
        }
        needed = config.minimum_completed_bars
        if len(bars) < needed:
            if len(bars) >= 2 and any(
                right["bucket"] - left["bucket"] != 1 for left, right in zip(bars, bars[1:])
            ):
                return base | {"reason": "incomplete_contiguous_minute_history"}
            return base | {"reason": "warming_up_insufficient_completed_bars"}
        bars = bars[-needed:]
        # A gap in the recent one-minute series invalidates crossover context.
        if any(right["bucket"] - left["bucket"] != 1 for left, right in zip(bars, bars[1:])):
            return base | {"reason": "incomplete_contiguous_minute_history"}

        latest_age = (current.astimezone(timezone.utc).timestamp() * 1000 - bars[-1]["close_ms"]) / 1000
        if latest_age < -2 or latest_age > config.max_signal_age_seconds:
            return base | {"reason": "completed_bar_is_stale"}

        if current_snapshot is not None:
            if current_snapshot.get("research_only") is not True or current_snapshot.get("execution") != "none":
                return base | {"reason": "snapshot_not_research_only"}
            if current_snapshot.get("entry_eligible") is not True or current_snapshot.get("close_only") is True:
                return base | {"reason": "instrument_not_entry_eligible"}
            if current_snapshot.get("settlement") is True:
                return base | {"reason": "instrument_settled"}

        closes = [row["close"] for row in bars]
        fast_prev = _ema(closes[:-1], config.fast_ema_period)
        slow_prev = _ema(closes[:-1], config.slow_ema_period)
        fast_now = _ema(closes, config.fast_ema_period)
        slow_now = _ema(closes, config.slow_ema_period)
        momentum_reference = closes[-1 - config.momentum_bars]
        momentum = closes[-1] / momentum_reference - ONE
        returns = [
            abs(closes[index] / closes[index - 1] - ONE)
            for index in range(len(closes) - config.volatility_lookback, len(closes))
        ]
        mean_abs_return = sum(returns, ZERO) / Decimal(len(returns))
        stop_distance = max(
            config.minimum_stop_distance_pct,
            min(config.maximum_stop_distance_pct, mean_abs_return * config.volatility_stop_multiple),
        )
        result = base | {
            "fast_ema": str(fast_now),
            "slow_ema": str(slow_now),
            "momentum_pct": str(momentum * Decimal("100")),
            "mean_abs_1m_return_pct": str(mean_abs_return * Decimal("100")),
            "stop_loss_distance_pct": str(stop_distance),
            "reward_risk_multiple": str(config.reward_risk_multiple),
            "entry_notional_usd": str(config.entry_notional_usd),
        }
        if not config.minimum_abs_volatility_pct <= mean_abs_return <= config.maximum_abs_volatility_pct:
            return result | {"reason": "volatility_out_of_bounds"}
        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now
        if crossed_up and momentum > ZERO:
            return result | {"signal": True, "side": "LONG", "reason": "bullish_ema_crossover_confirmed"}
        if crossed_down and momentum < ZERO:
            return result | {"signal": True, "side": "SHORT", "reason": "bearish_ema_crossover_confirmed"}
        return result | {"reason": "no_confirmed_ema_crossover"}


def strategy_from_env() -> PerpsEmaMomentumStrategy:
    """Load bounded research-only strategy settings from environment."""
    try:
        config = PerpsStrategyConfig(
            fast_ema_period=int(os.getenv("PERPS_STRATEGY_FAST_EMA", "8")),
            slow_ema_period=int(os.getenv("PERPS_STRATEGY_SLOW_EMA", "21")),
            minimum_completed_bars=int(os.getenv("PERPS_STRATEGY_MIN_BARS", "60")),
            momentum_bars=int(os.getenv("PERPS_STRATEGY_MOMENTUM_BARS", "5")),
            volatility_lookback=int(os.getenv("PERPS_STRATEGY_VOLATILITY_LOOKBACK", "14")),
            minimum_abs_volatility_pct=_decimal(os.getenv("PERPS_STRATEGY_MIN_VOLATILITY_PCT", "0.0002"), "minimum_abs_volatility_pct"),
            maximum_abs_volatility_pct=_decimal(os.getenv("PERPS_STRATEGY_MAX_VOLATILITY_PCT", "0.01"), "maximum_abs_volatility_pct"),
            volatility_stop_multiple=_decimal(os.getenv("PERPS_STRATEGY_STOP_VOL_MULTIPLE", "3"), "volatility_stop_multiple"),
            minimum_stop_distance_pct=_decimal(os.getenv("PERPS_STRATEGY_MIN_STOP_PCT", "0.005"), "minimum_stop_distance_pct"),
            maximum_stop_distance_pct=_decimal(os.getenv("PERPS_STRATEGY_MAX_STOP_PCT", "0.015"), "maximum_stop_distance_pct"),
            reward_risk_multiple=_decimal(os.getenv("PERPS_STRATEGY_REWARD_RISK", "2"), "reward_risk_multiple"),
            entry_notional_usd=_decimal(os.getenv("PERPS_AUTO_ENTRY_NOTIONAL_USD", "10"), "entry_notional_usd"),
            max_signal_age_seconds=int(os.getenv("PERPS_STRATEGY_MAX_SIGNAL_AGE_SECONDS", "120")),
        )
    except (ValueError, TypeError, OverflowError):
        raise ValueError("invalid_perps_strategy_configuration") from None
    return PerpsEmaMomentumStrategy(config)
