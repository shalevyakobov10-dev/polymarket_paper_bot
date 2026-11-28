"""Shared, strictly validated Perps paper-engine configuration.

Used by both the supervised paper worker and historical replay so research
runs cannot silently use different risk thresholds from the running simulator.
"""
from __future__ import annotations

import os
from typing import Mapping


def _bounded_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = env.get(name, str(default))
    if isinstance(raw, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def perps_engine_options_from_env(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str | int]:
    """Return the exact validated configuration shared by paper and replay."""
    env = os.environ if environ is None else environ
    return {
        "initial_equity": str(env.get("PERPS_PAPER_INITIAL_EQUITY", "1000")),
        "max_position_notional": str(env.get("PERPS_PAPER_MAX_POSITION_NOTIONAL", "25")),
        "max_total_notional": str(env.get("PERPS_PAPER_MAX_TOTAL_NOTIONAL", "75")),
        "max_open_positions": _bounded_int(env, "PERPS_PAPER_MAX_OPEN_POSITIONS", 3, 1, 10),
        "max_leverage": str(env.get("PERPS_PAPER_MAX_LEVERAGE", "2")),
        "risk_per_trade_pct": str(env.get("PERPS_PAPER_RISK_PER_TRADE_PCT", "0.0025")),
        "stop_loss_pct": str(env.get("PERPS_PAPER_STOP_LOSS_PCT", "0.0075")),
        "take_profit_pct": str(env.get("PERPS_PAPER_TAKE_PROFIT_PCT", "0.015")),
        # Only used as legacy defaults for migrated positions. New entries require
        # a current public fee schedule, and instrument liquidation fees are used.
        "taker_fee_rate": str(env.get("PERPS_PAPER_LEGACY_TAKER_FEE_RATE", "0.0005")),
        "liquidation_fee_rate": str(env.get("PERPS_PAPER_LEGACY_LIQUIDATION_FEE_RATE", "0.01")),
        "max_spread_bps": str(env.get("PERPS_PAPER_MAX_SPREAD_BPS", "20")),
        "lookback_observations": _bounded_int(
            env, "PERPS_PAPER_LOOKBACK_OBSERVATIONS", 10, 3, 500
        ),
        "momentum_threshold_bps": str(env.get("PERPS_PAPER_MOMENTUM_THRESHOLD_BPS", "20")),
        "minimum_history_span_seconds": _bounded_int(
            env, "PERPS_PAPER_MIN_HISTORY_SPAN_SECONDS", 270, 0, 86400
        ),
        "daily_drawdown_pct": str(env.get("PERPS_PAPER_DAILY_DRAWDOWN_PCT", "0.03")),
        "maximum_drawdown_pct": str(env.get("PERPS_PAPER_MAXIMUM_DRAWDOWN_PCT", "0.10")),
        "max_data_age_seconds": _bounded_int(
            env, "PERPS_PAPER_MAX_DATA_AGE_SECONDS", 15, 1, 60
        ),
        "max_entry_impact_bps": str(env.get("PERPS_PAPER_MAX_ENTRY_IMPACT_BPS", "50")),
        "funding_schedule_grace_seconds": _bounded_int(
            env, "PERPS_PAPER_FUNDING_SCHEDULE_GRACE_SECONDS", 60, 5, 300
        ),
        "max_post_funding_mark_delay_seconds": _bounded_int(
            env, "PERPS_PAPER_MAX_POST_FUNDING_MARK_DELAY_SECONDS", 90, 15, 120
        ),
    }
