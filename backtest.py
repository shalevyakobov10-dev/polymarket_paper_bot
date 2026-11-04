"""Historical, read-only backtest for the paper strategy.

This script uses public market history only. It does not place orders and does
not claim profitability. Time-series splits are chronological.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from paper_trading import PaperConfig, PaperTrader

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
USER_AGENT = "PolymarketPaperBacktest/1.0"
MINIMUM_CLOSED_TRADES_FOR_DESCRIPTIVE_METRICS = 30


def trade_sample_quality(closed_trades: int) -> dict[str, Any]:
    """Label sparse trade samples without implying statistical significance."""
    count = int(closed_trades)
    adequate = count >= MINIMUM_CLOSED_TRADES_FOR_DESCRIPTIVE_METRICS
    return {
        "closed_trades": count,
        "minimum_closed_trades_for_descriptive_metrics": MINIMUM_CLOSED_TRADES_FOR_DESCRIPTIVE_METRICS,
        "status": "MINIMUM_SAMPLE_REACHED" if adequate else "BELOW_MINIMUM_SAMPLE",
        "note": (
            "This is only a reporting floor, not proof of predictive edge or statistical significance."
        ),
    }


def get_json(url: str, timeout: int = 20) -> Any:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status} returned by {url.split('?')[0]}")
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except OSError:
            pass
        raise RuntimeError(f"HTTP {exc.code} from {url.split('?')[0]}: {detail}") from exc


def list_field(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def discover_tokens(markets_limit: int) -> list[dict[str, str]]:
    query = urlencode({"active": "true", "closed": "false", "limit": max(20, min(500, markets_limit * 20)), "offset": 0})
    payload = get_json(f"{GAMMA_BASE}/markets?{query}")
    if not isinstance(payload, list):
        raise ValueError("Gamma API did not return a market list")
    candidates = []
    for market in payload:
        if not isinstance(market, dict):
            continue
        token_ids = list_field(market.get("clobTokenIds"))
        outcomes = list_field(market.get("outcomes"))
        prices = list_field(market.get("outcomePrices"))
        volume = number(market.get("volume24hr")) or 0.0
        for i, token in enumerate(token_ids):
            token_id = str(token).strip()
            if not token_id:
                continue
            candidates.append({
                "token_id": token_id,
                "market_id": str(market.get("conditionId") or market.get("id") or ""),
                "question": str(market.get("question") or "Unknown market")[:500],
                "outcome": str(outcomes[i]) if i < len(outcomes) else f"Outcome {i + 1}",
                "volume_24h": str(volume),
            })
    # Sort markets by volume, deduplicate token IDs, and then cap token count.
    candidates.sort(key=lambda item: float(item["volume_24h"]), reverse=True)
    distinct, seen = [], set()
    for item in candidates:
        if item["token_id"] in seen:
            continue
        seen.add(item["token_id"])
        distinct.append(item)
        if len(distinct) >= markets_limit * 2:
            break
    if not distinct:
        raise RuntimeError("No token IDs were found in active Gamma markets")
    return distinct


def fetch_price_history(token: dict[str, str], start_ts: int, end_ts: int, fidelity_minutes: int) -> list[dict[str, Any]]:
    # The current CLOB endpoint supports either an interval or explicit timestamp
    # bounds. Use interval=max to avoid a 400 from mixed query validation, then
    # apply the desired window locally. The requested fidelity limits data density.
    params = urlencode({
        "market": token["token_id"],
        "interval": "max",
        "fidelity": fidelity_minutes,
    })
    payload = get_json(f"{CLOB_BASE}/prices-history?{params}")
    if not isinstance(payload, dict):
        raise ValueError("CLOB price history response was not an object")
    history = payload.get("history")
    if not isinstance(history, list):
        raise ValueError("CLOB price history response did not contain a history list")
    parsed = []
    for point in history:
        if not isinstance(point, dict):
            continue
        try:
            stamp = int(point["t"])
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
        price = number(point.get("p"))
        if price is None or not 0.0 < price < 1.0:
            continue
        if stamp < start_ts or stamp > end_ts:
            continue
        parsed.append({
            "timestamp": stamp,
            "token_id": token["token_id"],
            "market_id": token["market_id"],
            "question": token["question"],
            "outcome": token["outcome"],
            "observed_price": price,
        })
    parsed.sort(key=lambda row: row["timestamp"])
    return parsed


def utc_from_timestamp(stamp: int) -> datetime:
    return datetime.fromtimestamp(stamp, tz=timezone.utc)


def evaluate_forward_returns(
    rows: list[dict[str, Any]],
    threshold: float,
    horizons_hours: tuple[int, ...] = (1, 6, 24),
    prior_prices: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Compare momentum and contrarian-long event returns with a same-window baseline.

    Positive moves represent a long-after-rise (momentum) hypothesis. Negative
    moves represent a long-after-drop (contrarian/rebound) hypothesis. The
    forward point is the first recorded observation at or after the chosen
    horizon. Signals and benchmarks without a full future point are omitted
    from return statistics. This is a descriptive event study, not a strategy
    simulation, and neither direction is selected or promoted automatically.
    """
    threshold_value = number(threshold)
    if isinstance(threshold, bool) or threshold_value is None or not 0 < threshold_value < 1:
        raise ValueError("threshold must be finite and in (0, 1)")
    threshold = threshold_value
    if not horizons_hours or any(
        isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0
        for horizon in horizons_hours
    ):
        raise ValueError("horizons_hours must contain positive integers")

    by_token: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        token_id = str(row.get("token_id") or "")
        price = number(row.get("observed_price"))
        timestamp = number(row.get("timestamp"))
        if token_id and price is not None and 0 < price < 1 and timestamp is not None:
            by_token[token_id].append({"timestamp": int(timestamp), "price": price})
    for token_rows in by_token.values():
        token_rows.sort(key=lambda row: row["timestamp"])

    output = {}
    for horizon in horizons_hours:
        horizon_seconds = int(horizon * 3600)
        positive_returns: list[float] = []
        negative_returns: list[float] = []
        baseline_returns: list[float] = []
        positive_signals_seen = 0
        negative_signals_seen = 0

        for token_id, token_rows in by_token.items():
            seed = number((prior_prices or {}).get(token_id))
            previous_price = seed
            future_index = 1
            count = len(token_rows)
            for index, row in enumerate(token_rows):
                current_price = float(row["price"])
                target_timestamp = int(row["timestamp"]) + horizon_seconds
                if future_index < index + 1:
                    future_index = index + 1
                while (
                    future_index < count
                    and int(token_rows[future_index]["timestamp"]) < target_timestamp
                ):
                    future_index += 1
                future_return = None
                if future_index < count:
                    future_return = float(token_rows[future_index]["price"]) / current_price - 1.0
                    baseline_returns.append(future_return)

                if previous_price is not None:
                    price_delta = current_price - previous_price
                    if price_delta >= threshold:
                        positive_signals_seen += 1
                        if future_return is not None:
                            positive_returns.append(future_return)
                    elif price_delta <= -threshold:
                        negative_signals_seen += 1
                        if future_return is not None:
                            negative_returns.append(future_return)
                previous_price = current_price

        def summarize(values: list[float]) -> dict[str, Any]:
            if not values:
                return {
                    "n": 0,
                    "mean_return_pct": None,
                    "median_return_pct": None,
                    "positive_rate_pct": None,
                }
            ordered = sorted(values)
            middle = len(ordered) // 2
            median = (
                ordered[middle]
                if len(ordered) % 2
                else (ordered[middle - 1] + ordered[middle]) / 2
            )
            return {
                "n": len(values),
                "mean_return_pct": round(100 * sum(values) / len(values), 5),
                "median_return_pct": round(100 * median, 5),
                "positive_rate_pct": round(
                    100 * sum(1 for value in values if value > 0) / len(values), 3
                ),
            }

        def excess_mean(
            signal_summary: dict[str, Any],
            baseline_summary: dict[str, Any],
        ) -> float | None:
            signal_mean = signal_summary["mean_return_pct"]
            baseline_mean = baseline_summary["mean_return_pct"]
            if signal_mean is None or baseline_mean is None:
                return None
            return round(signal_mean - baseline_mean, 5)

        positive_summary = summarize(positive_returns)
        negative_summary = summarize(negative_returns)
        baseline_summary = summarize(baseline_returns)
        output[f"{horizon}h"] = {
            # Backward-compatible aliases for the original positive-move study.
            "signals_detected": positive_signals_seen,
            "signal_forward_returns": positive_summary,
            "signal_minus_baseline_mean_return_pct": excess_mean(
                positive_summary, baseline_summary
            ),
            "positive_move_signals_detected": positive_signals_seen,
            "positive_move_forward_returns": positive_summary,
            "positive_move_minus_baseline_mean_return_pct": excess_mean(
                positive_summary, baseline_summary
            ),
            "negative_move_signals_detected": negative_signals_seen,
            "negative_move_long_forward_returns": negative_summary,
            "negative_move_long_minus_baseline_mean_return_pct": excess_mean(
                negative_summary, baseline_summary
            ),
            "all_observation_baseline": baseline_summary,
        }
    return {
        "threshold": float(threshold),
        "method": (
            "first observed price at or after horizon; only full in-window horizons; "
            "no executable fill assumptions"
        ),
        "directional_hypotheses": {
            "positive_move": "long_after_price_rise_momentum",
            "negative_move": "long_after_price_drop_contrarian_rebound",
        },
        "interpretation": (
            "Descriptive research only. Both directions are reported against the same "
            "all-observation baseline; small event counts are not evidence of an edge. "
            "No direction is selected, promoted, or connected to paper entries."
        ),
        "horizons": output,
        "research_only": True,
        "execution": "none",
    }



def diagnose_price_changes(rows: list[dict[str, Any]], thresholds: tuple[float, ...] = (0.005, 0.01, 0.02, 0.03)) -> dict[str, Any]:
    """Describe observed moves without trading or selecting a winning threshold."""
    by_token: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        token_id = str(row.get("token_id") or "")
        price = number(row.get("observed_price"))
        if token_id and price is not None and 0 < price < 1:
            by_token[token_id].append(row)
    counts = {f"{threshold:.4f}": 0 for threshold in thresholds}
    up_moves, down_moves = [], []
    for token_rows in by_token.values():
        token_rows.sort(key=lambda row: int(row["timestamp"]))
        previous = None
        for row in token_rows:
            price = float(row["observed_price"])
            if previous is not None:
                delta = price - previous
                if delta > 0:
                    up_moves.append(delta)
                    for threshold in thresholds:
                        if delta >= threshold:
                            counts[f"{threshold:.4f}"] += 1
                elif delta < 0:
                    down_moves.append(abs(delta))
            previous = price
    total = len(up_moves) + len(down_moves)
    return {
        "consecutive_price_pairs": total,
        "positive_moves_by_minimum_delta": counts,
        "largest_positive_delta": round(max(up_moves), 8) if up_moves else 0.0,
        "largest_negative_delta_abs": round(max(down_moves), 8) if down_moves else 0.0,
        "interpretation": "Raw observed price changes only; counts are not trade counts or profit estimates.",
    }


def group_by_timestamp(rows: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        result[int(row["timestamp"])].append(row)
    return dict(sorted(result.items()))


def clean_history_rows(histories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize valid timestamped observations and reject malformed input rows."""
    cleaned = []
    for row in histories:
        if not isinstance(row, dict):
            continue
        price = number(row.get("observed_price"))
        timestamp = number(row.get("timestamp"))
        if price is None or not 0.0 < price < 1.0 or timestamp is None:
            continue
        if not timestamp.is_integer():
            continue
        normalized = {**row, "timestamp": int(timestamp), "observed_price": float(price)}
        if normalized.get("token_id") is not None:
            normalized["token_id"] = str(normalized["token_id"])
        cleaned.append(normalized)
    return cleaned


def run_segment(
    rows: list[dict[str, Any]],
    config: PaperConfig,
    state_path: Path,
    prime_prices: list[dict[str, Any]] | None = None,
    prime_timestamp: datetime | None = None,
) -> dict[str, Any]:
    trader = PaperTrader(state_path, config)
    if prime_prices:
        # Seed only the last observed price before the test window. This prevents
        # the first test candle from creating a false "first observation" gap.
        trader.process_snapshot(prime_prices, prime_timestamp, persist_state=False)
    events = group_by_timestamp(rows)
    candidates = opened = closed = 0
    equity_peak = float(config.starting_cash)
    max_drawdown_usd = 0.0
    max_drawdown_pct = 0.0
    for stamp, snapshot_rows in events.items():
        result = trader.process_snapshot(snapshot_rows, utc_from_timestamp(stamp), persist_state=False)
        candidates += len(result["candidates"])
        opened += len(result["opened"])
        closed += len(result["closed"])
        equity = float(trader.snapshot()["metrics"]["equity"])
        equity_peak = max(equity_peak, equity)
        drawdown_usd = max(0.0, equity_peak - equity)
        max_drawdown_usd = max(max_drawdown_usd, drawdown_usd)
        if equity_peak > 0.0:
            max_drawdown_pct = max(max_drawdown_pct, 100.0 * drawdown_usd / equity_peak)
    status = trader.snapshot()
    return {
        "window_start_utc": utc_from_timestamp(min(events)).isoformat() if events else None,
        "window_end_utc": utc_from_timestamp(max(events)).isoformat() if events else None,
        "snapshots": len(events),
        "observations": len(rows),
        "candidate_signals": candidates,
        "positions_opened": opened,
        "positions_closed": closed,
        "metrics": status["metrics"],
        "open_positions_at_end": len(status["positions_list"]),
        "max_drawdown_usd": round(max_drawdown_usd, 4),
        "max_drawdown_pct": round(max_drawdown_pct, 4),
        "drawdown_method": "snapshot-marked peak-to-trough; intraperiod drawdown may be missed",
        "trade_sample_quality": trade_sample_quality(int(status["metrics"]["closed_trades"])),
    }


def run_threshold_sensitivity(
    histories: list[dict[str, Any]],
    base_config: PaperConfig,
    train_ratio: float,
    thresholds: tuple[float, ...] = (0.005, 0.01, 0.02, 0.03),
) -> list[dict[str, Any]]:
    """Compare predeclared signal thresholds on the exact same history window."""
    results = []
    for threshold in thresholds:
        trial_config = PaperConfig(**{**base_config.__dict__, "signal_delta": threshold})
        trial = run_backtest(histories, trial_config, train_ratio)
        results.append({
            "signal_delta": threshold,
            "forward_return_study": trial["forward_return_study"],
            "in_sample": {
                "candidate_signals": trial["in_sample"]["candidate_signals"],
                "positions_opened": trial["in_sample"]["positions_opened"],
                "positions_closed": trial["in_sample"]["positions_closed"],
                "return_pct": trial["in_sample"]["metrics"]["return_pct"],
                "closed_trades": trial["in_sample"]["metrics"]["closed_trades"],
                "win_rate_pct": trial["in_sample"]["metrics"]["win_rate_pct"],
                "trade_sample_quality": trial["in_sample"]["trade_sample_quality"],
            },
            "out_of_sample": {
                "candidate_signals": trial["out_of_sample"]["candidate_signals"],
                "positions_opened": trial["out_of_sample"]["positions_opened"],
                "positions_closed": trial["out_of_sample"]["positions_closed"],
                "return_pct": trial["out_of_sample"]["metrics"]["return_pct"],
                "closed_trades": trial["out_of_sample"]["metrics"]["closed_trades"],
                "win_rate_pct": trial["out_of_sample"]["metrics"]["win_rate_pct"],
                "trade_sample_quality": trial["out_of_sample"]["trade_sample_quality"],
            },
        })
    return results


def run_execution_cost_sensitivity(
    histories: list[dict[str, Any]],
    base_config: PaperConfig,
    train_ratio: float = 0.7,
    *,
    fee_rates: tuple[float, ...] = (0.0, 0.005, 0.01),
    slippage_rates: tuple[float, ...] = (0.005, 0.01, 0.02),
) -> list[dict[str, Any]]:
    """Replay the same fixed strategy over a predeclared fee/slippage stress grid.

    This is a robustness diagnostic, not a way to optimize costs or choose a
    profitable setting. Each scenario reuses identical price history and the
    same chronological split; it changes only assumed costs.
    """
    if not fee_rates or not slippage_rates:
        raise ValueError("fee_rates and slippage_rates must be non-empty")
    for rate in (*fee_rates, *slippage_rates):
        if not math.isfinite(float(rate)) or not 0.0 <= float(rate) < 0.5:
            raise ValueError("all fee and slippage assumptions must be finite and in [0, 0.5)")
    results = []
    for fee_rate in fee_rates:
        for slippage_rate in slippage_rates:
            scenario_config = PaperConfig(**{
                **base_config.__dict__,
                "fee_rate": float(fee_rate),
                "simulated_slippage": float(slippage_rate),
            })
            report = run_backtest(histories, scenario_config, train_ratio)
            results.append({
                "fee_rate_assumption_each_side": float(fee_rate),
                "slippage_rate_each_side": float(slippage_rate),
                "in_sample": {
                    "return_pct": report["in_sample"]["metrics"]["return_pct"],
                    "closed_trades": report["in_sample"]["metrics"]["closed_trades"],
                    "win_rate_pct": report["in_sample"]["metrics"]["win_rate_pct"],
                },
                "out_of_sample": {
                    "return_pct": report["out_of_sample"]["metrics"]["return_pct"],
                    "closed_trades": report["out_of_sample"]["metrics"]["closed_trades"],
                    "win_rate_pct": report["out_of_sample"]["metrics"]["win_rate_pct"],
                    "ending_equity": report["out_of_sample"]["metrics"]["equity"],
                },
                "same_strategy_and_history": True,
                "execution": "none",
                "research_only": True,
            })
    return results


def run_backtest(
    histories: list[dict[str, Any]],
    config: PaperConfig,
    train_ratio: float = 0.7,
) -> dict[str, Any]:
    if not 0.5 <= train_ratio < 0.9:
        raise ValueError("train_ratio must be between 0.5 and 0.9")
    cleaned = clean_history_rows(histories)
    stamps = sorted({int(row["timestamp"]) for row in cleaned})
    if len(stamps) < 8:
        raise ValueError(f"Need at least 8 unique time points; got {len(stamps)}")
    split_idx = int(len(stamps) * train_ratio)
    split_idx = min(max(2, split_idx), len(stamps) - 2)
    split_ts = stamps[split_idx]
    train_rows = [row for row in cleaned if int(row["timestamp"]) < split_ts]
    test_rows = [row for row in cleaned if int(row["timestamp"]) >= split_ts]
    if not train_rows or not test_rows:
        raise ValueError("Chronological split produced an empty segment")
    # Use separate fresh portfolios for both segments to prevent train P&L or
    # positions from leaking into the held-out test results.
    with tempfile.TemporaryDirectory(prefix="polymarket-paper-backtest-") as temp_dir:
        root = Path(temp_dir)
        train = run_segment(train_rows, config, root / "train_state.json")
        pre_split_by_token = {}
        for row in train_rows:
            key = str(row.get("token_id") or "")
            if not key:
                continue
            if key not in pre_split_by_token or int(row["timestamp"]) > int(pre_split_by_token[key]["timestamp"]):
                pre_split_by_token[key] = row
        prime = [{
            **row,
            "observed_price": float(row["observed_price"]),
        } for row in pre_split_by_token.values()]
        prior_prices = {token_id: float(row["observed_price"]) for token_id, row in pre_split_by_token.items()}
        test = run_segment(
            test_rows,
            config,
            root / "test_state.json",
            prime_prices=prime,
            prime_timestamp=utc_from_timestamp(split_ts) - timedelta(microseconds=1),
        )
    forward_study = {
        "in_sample": evaluate_forward_returns(train_rows, config.signal_delta),
        "out_of_sample": evaluate_forward_returns(test_rows, config.signal_delta, prior_prices=prior_prices),
    }
    warnings = []
    if train["positions_opened"] == 0:
        warnings.append("No simulated positions opened in the in-sample segment for this threshold.")
    if test["positions_opened"] == 0:
        warnings.append("No simulated positions opened in the out-of-sample segment for this threshold.")
    for label, segment in (("in-sample", train), ("out-of-sample", test)):
        closed = int(segment["metrics"]["closed_trades"])
        if closed < MINIMUM_CLOSED_TRADES_FOR_DESCRIPTIVE_METRICS:
            warnings.append(
                f"The {label} segment has only {closed} closed trades; "
                f"at least {MINIMUM_CLOSED_TRADES_FOR_DESCRIPTIVE_METRICS} are required to meet the basic descriptive sample floor."
            )
    change_diagnostics = {
        "in_sample": diagnose_price_changes(train_rows),
        "out_of_sample": diagnose_price_changes(test_rows),
    }
    return {
        "mode": "HISTORICAL_PAPER_BACKTEST",
        "strategy": {
            "entry_rule": f"observed price increase >= {config.signal_delta:.4f} probability points between history observations",
            "exit_rule": f"simulated stop loss {config.stop_loss_pct:.1%}, take profit {config.take_profit_pct:.1%}, or max hold {config.max_hold_hours:g} hours",
            "starting_cash_usd_each_segment": config.starting_cash,
            "max_position_usd": config.max_position_usd,
            "max_open_exposure_usd": config.max_open_exposure_usd,
            "max_open_positions": config.max_open_positions,
            "slippage_rate_each_side": config.simulated_slippage,
            "fee_rate_assumption": config.fee_rate,
        },
        "data": {
            "unique_timestamps": len(stamps),
            "price_observations": len(cleaned),
            "unique_tokens": len({str(row.get("token_id") or "") for row in cleaned}),
            "train_ratio": train_ratio,
            "history_fidelity_minutes": None,
            "split_time_utc": utc_from_timestamp(split_ts).isoformat(),
            "split_method": "chronological",
        },
        "in_sample": train,
        "out_of_sample": test,
        "evaluation_quality": {
            "in_sample": train["trade_sample_quality"],
            "out_of_sample": test["trade_sample_quality"],
            "interpretation": (
                "The minimum trade count is a basic reporting screen only; even reaching it "
                "does not establish statistical significance, predictive edge, or profitability."
            ),
        },
        "forward_return_study": forward_study,
        "price_change_diagnostics": change_diagnostics,
        "warnings": warnings,
        "limitations": [
            "Historical prices are observed candles; they do not reproduce historical order-book depth or executable fills.",
            "The strategy is an experimental price-change rule, not a validated predictive model.",
            "Open positions at each segment end are marked, not settled on the eventual market resolution.",
            "The default fee is zero unless the user supplies a conservative fee assumption.",
            "A good backtest does not guarantee future performance; evaluate across many markets and time periods.",
        ],
    }


def run_walk_forward_backtest(
    histories: list[dict[str, Any]],
    config: PaperConfig,
    *,
    folds: int = 5,
    initial_train_ratio: float = 0.5,
) -> dict[str, Any]:
    """Evaluate one fixed paper strategy across expanding, non-overlapping OOS windows.

    Every fold starts with a fresh simulated portfolio. Its only state carried
    from the past is the latest observed price per token, used to avoid a false
    first-observation signal. No training P&L, open position, or future price
    is carried into a test fold.
    """
    if isinstance(folds, bool) or not isinstance(folds, int) or not 2 <= folds <= 10:
        raise ValueError("folds must be an integer between 2 and 10")
    if not math.isfinite(float(initial_train_ratio)) or not 0.3 <= float(initial_train_ratio) < 0.9:
        raise ValueError("initial_train_ratio must be between 0.3 and 0.9")

    cleaned = clean_history_rows(histories)
    stamps = sorted({int(row["timestamp"]) for row in cleaned})
    if len(stamps) < 8:
        raise ValueError(f"Need at least 8 unique time points; got {len(stamps)}")
    initial_train_count = int(len(stamps) * float(initial_train_ratio))
    if initial_train_count < 4:
        raise ValueError("initial training window must contain at least 4 unique time points")
    remaining_count = len(stamps) - initial_train_count
    if remaining_count < folds * 2:
        raise ValueError(
            "not enough remaining timestamps; each out-of-sample fold requires at least 2 unique timestamps"
        )

    # Split the remaining timeline into consecutive, non-overlapping test blocks.
    base_size, extra = divmod(remaining_count, folds)
    fold_sizes = [base_size + (1 if index < extra else 0) for index in range(folds)]
    results: list[dict[str, Any]] = []
    cursor = initial_train_count

    with tempfile.TemporaryDirectory(prefix="polymarket-walk-forward-") as temp_dir:
        root = Path(temp_dir)
        for fold_number, fold_size in enumerate(fold_sizes, start=1):
            test_stamps = stamps[cursor:cursor + fold_size]
            cursor += fold_size
            test_start = test_stamps[0]
            test_end = test_stamps[-1]
            train_rows = [row for row in cleaned if int(row["timestamp"]) < test_start]
            test_rows = [
                row for row in cleaned
                if test_start <= int(row["timestamp"]) <= test_end
            ]
            if not train_rows or not test_rows:
                raise ValueError(f"fold {fold_number} produced an empty train or test window")

            latest_by_token: dict[str, dict[str, Any]] = {}
            for row in train_rows:
                token_id = str(row.get("token_id") or "")
                if not token_id:
                    continue
                prior = latest_by_token.get(token_id)
                if prior is None or int(row["timestamp"]) > int(prior["timestamp"]):
                    latest_by_token[token_id] = row
            prime_prices = [
                {**row, "observed_price": float(row["observed_price"])}
                for row in latest_by_token.values()
            ]
            segment = run_segment(
                test_rows,
                config,
                root / f"fold_{fold_number}_state.json",
                prime_prices=prime_prices,
                prime_timestamp=utc_from_timestamp(test_start) - timedelta(microseconds=1),
            )
            prior_prices = {
                token_id: float(row["observed_price"])
                for token_id, row in latest_by_token.items()
            }
            directional_forward_return_study = evaluate_forward_returns(
                test_rows,
                config.signal_delta,
                prior_prices=prior_prices,
            )
            train_stamps = sorted({int(row["timestamp"]) for row in train_rows})
            results.append({
                "fold": fold_number,
                "training_window": {
                    "start_utc": utc_from_timestamp(train_stamps[0]).isoformat(),
                    "end_utc": utc_from_timestamp(train_stamps[-1]).isoformat(),
                    "unique_timestamps": len(train_stamps),
                    "observations": len(train_rows),
                },
                "test_window": {
                    "start_utc": utc_from_timestamp(test_start).isoformat(),
                    "end_utc": utc_from_timestamp(test_end).isoformat(),
                    "unique_timestamps": len(test_stamps),
                    "observations": len(test_rows),
                },
                "out_of_sample": segment,
                "directional_forward_return_study": directional_forward_return_study,
                "research_only": True,
                "execution": "none",
            })

    returns = [
        float(value)
        for fold in results
        if (value := number(fold["out_of_sample"]["metrics"].get("return_pct"))) is not None
    ]
    sorted_returns = sorted(returns)
    median_return = None
    if sorted_returns:
        middle = len(sorted_returns) // 2
        median_return = (
            sorted_returns[middle]
            if len(sorted_returns) % 2
            else (sorted_returns[middle - 1] + sorted_returns[middle]) / 2.0
        )
    total_closed = sum(int(fold["out_of_sample"]["positions_closed"]) for fold in results)
    total_opened = sum(int(fold["out_of_sample"]["positions_opened"]) for fold in results)
    total_observations = sum(int(fold["test_window"]["observations"]) for fold in results)
    oos_metrics = [fold["out_of_sample"]["metrics"] for fold in results]
    weighted_win_rate = (
        sum(float(item["win_rate_pct"]) * int(item["closed_trades"]) for item in oos_metrics) / total_closed
        if total_closed else None
    )
    fold_drawdowns = [float(fold["out_of_sample"]["max_drawdown_pct"]) for fold in results]
    trade_active_folds = [
        fold for fold in results
        if int(fold["out_of_sample"]["positions_closed"]) > 0
    ]
    trade_active_returns = [
        float(fold["out_of_sample"]["metrics"]["return_pct"])
        for fold in trade_active_folds
    ]
    sorted_active_returns = sorted(trade_active_returns)
    active_median_return = None
    if sorted_active_returns:
        middle = len(sorted_active_returns) // 2
        active_median_return = (
            sorted_active_returns[middle]
            if len(sorted_active_returns) % 2
            else (sorted_active_returns[middle - 1] + sorted_active_returns[middle]) / 2.0
        )
    warnings = []
    if total_closed == 0:
        warnings.append("No positions closed across the walk-forward test folds; return consistency is not established.")
    if total_opened == 0:
        warnings.append("No simulated positions opened across the walk-forward test folds.")
    if total_closed < MINIMUM_CLOSED_TRADES_FOR_DESCRIPTIVE_METRICS:
        warnings.append(
            f"Only {total_closed} closed trades were observed across all walk-forward test folds; "
            f"at least {MINIMUM_CLOSED_TRADES_FOR_DESCRIPTIVE_METRICS} are required to meet the basic descriptive sample floor."
        )

    return {
        "mode": "EXPANDING_WALK_FORWARD_PAPER_BACKTEST",
        "fold_count": folds,
        "initial_train_ratio": float(initial_train_ratio),
        "strategy_fixed_across_folds": True,
        "test_windows_non_overlapping": True,
        "portfolio_reset_each_fold": True,
        "data": {
            "unique_timestamps": len(stamps),
            "price_observations": len(cleaned),
            "unique_tokens": len({str(row.get("token_id") or "") for row in cleaned}),
            "initial_train_timestamps": initial_train_count,
            "out_of_sample_timestamps": remaining_count,
        },
        "folds": results,
        "aggregate_out_of_sample": {
            "folds_with_return_metric": len(returns),
            "mean_fold_return_pct": round(sum(returns) / len(returns), 5) if returns else None,
            "median_fold_return_pct": round(median_return, 5) if median_return is not None else None,
            "profitable_fold_rate_pct": round(100 * sum(value > 0 for value in returns) / len(returns), 3) if returns else None,
            "total_test_observations": total_observations,
            "positions_opened": total_opened,
            "positions_closed": total_closed,
            "folds_with_closed_trades": len(trade_active_folds),
            "folds_without_closed_trades": len(results) - len(trade_active_folds),
            "mean_trade_active_fold_return_pct": round(sum(trade_active_returns) / len(trade_active_returns), 5) if trade_active_returns else None,
            "median_trade_active_fold_return_pct": round(active_median_return, 5) if active_median_return is not None else None,
            "profitable_trade_active_fold_rate_pct": round(100 * sum(value > 0 for value in trade_active_returns) / len(trade_active_returns), 3) if trade_active_returns else None,
            "trade_sample_quality": trade_sample_quality(total_closed),
            "weighted_win_rate_pct": round(weighted_win_rate, 4) if weighted_win_rate is not None else None,
            "mean_fold_max_drawdown_pct": round(sum(fold_drawdowns) / len(fold_drawdowns), 4) if fold_drawdowns else None,
            "worst_fold_max_drawdown_pct": round(max(fold_drawdowns), 4) if fold_drawdowns else None,
        },
        "warnings": warnings,
        "methodology": [
            "Expanding chronological training history with consecutive, non-overlapping out-of-sample test windows.",
            "The strategy configuration is fixed before all folds; no fold is used to select parameters.",
            "Each test fold uses a fresh paper portfolio and is primed only with the last pre-test price per token.",
            "Fold returns are equal-weight descriptive summaries, not a compounded portfolio return.",
            "Observed candles do not reproduce historical order-book depth, exact fills, or market-resolution payouts.",
        ],
        "research_only": True,
        "execution": "none",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only historical Polymarket paper backtest")
    parser.add_argument("--tokens", nargs="+", help="Explicit CLOB token IDs; if omitted, discover liquid active markets")
    parser.add_argument("--markets", type=int, default=20, help="Number of markets to discover (default: 20; up to two outcomes each)")
    parser.add_argument("--days", type=int, default=30, help="History lookback in days (default: 30)")
    parser.add_argument("--fidelity", type=int, default=5, help="History sampling resolution in minutes (default: 5; closer to the live polling cadence)")
    parser.add_argument("--train-ratio", type=float, default=0.7, help="Chronological in-sample share (default: 0.7)")
    parser.add_argument("--output", default="backtest_report.json", help="Output JSON file path")
    parser.add_argument("--signal-delta", type=float, default=0.03)
    parser.add_argument("--starting-cash", type=float, default=100.0)
    parser.add_argument("--position-size", type=float, default=2.0)
    parser.add_argument("--max-exposure", type=float, default=10.0)
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--slippage", type=float, default=0.005)
    parser.add_argument("--fee-rate", type=float, default=0.0)
    parser.add_argument("--sensitivity", action="store_true", help="Compare predeclared signal thresholds on the same history window")
    parser.add_argument("--cost-sensitivity", action="store_true", help="Stress-test predeclared fee and slippage assumptions on the same history")
    parser.add_argument("--walk-forward", action="store_true", help="Run expanding chronological out-of-sample paper evaluation")
    parser.add_argument("--walk-forward-folds", type=int, default=5, help="Number of non-overlapping walk-forward test windows (2–10; default: 5)")
    parser.add_argument("--walk-forward-initial-train-ratio", type=float, default=0.5, help="Initial history share used before the first walk-forward test window (default: 0.5)")
    args = parser.parse_args()

    if args.days < 1 or args.days > 365:
        parser.error("--days must be between 1 and 365")
    if args.fidelity < 1 or args.fidelity > 1440:
        parser.error("--fidelity must be between 1 and 1440 minutes")
    if args.markets < 1 or args.markets > 100:
        parser.error("--markets must be between 1 and 100")
    if args.max_positions < 1:
        parser.error("--max-positions must be at least 1")

    now_ts = int(datetime.now(timezone.utc).timestamp())
    start_ts = now_ts - args.days * 86400
    if args.tokens:
        tokens = [{"token_id": token, "market_id": "", "question": "Explicit token", "outcome": "Unknown"} for token in args.tokens]
    else:
        tokens = discover_tokens(args.markets)

    histories = []
    errors = []
    for idx, token in enumerate(tokens):
        try:
            history = fetch_price_history(token, start_ts, now_ts, args.fidelity)
            histories.extend(history)
        except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError, OSError) as exc:
            errors.append({"token_id": token["token_id"], "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
        # Be polite with public APIs and avoid bursts of token-history requests.
        if idx + 1 < len(tokens):
            time.sleep(0.12)

    if not histories:
        raise SystemExit(f"No usable historical price points were fetched. Errors: {json.dumps(errors)}")

    config = PaperConfig(
        starting_cash=args.starting_cash,
        max_position_usd=args.position_size,
        max_open_exposure_usd=args.max_exposure,
        max_open_positions=args.max_positions,
        signal_delta=args.signal_delta,
        simulated_slippage=args.slippage,
        fee_rate=args.fee_rate,
    )
    report = run_backtest(histories, config, args.train_ratio)
    report["source"] = "public Gamma market discovery and public CLOB /prices-history endpoint"
    report["data"]["market_universe_selection"] = (
        "explicit_token_ids" if args.tokens else
        "current_active_gamma_markets_ranked_by_current_reported_24h_volume"
    )
    if not args.tokens:
        report["limitations"].append(
            "The default universe is selected from currently active markets using current reported 24-hour volume; "
            "resolved/delisted markets and historical point-in-time membership are not reconstructed, so "
            "survivorship and selection bias may affect historical results."
        )
    report["fetched_tokens"] = len(tokens)
    report["data"]["history_fidelity_minutes"] = args.fidelity
    report["data"]["lookback_days"] = args.days
    if args.sensitivity:
        thresholds = tuple(sorted({0.005, 0.01, 0.02, 0.03, args.signal_delta}))
        report["threshold_sensitivity"] = run_threshold_sensitivity(histories, config, args.train_ratio, thresholds)
        report["sensitivity_note"] = "Exploratory threshold comparison on one sample; do not choose a threshold from holdout results and treat that same holdout as untouched. Validate selected settings on a later forward paper period."
    if args.walk_forward:
        report["walk_forward_evaluation"] = run_walk_forward_backtest(
            histories,
            config,
            folds=args.walk_forward_folds,
            initial_train_ratio=args.walk_forward_initial_train_ratio,
        )
    if args.cost_sensitivity:
        report["execution_cost_sensitivity"] = run_execution_cost_sensitivity(histories, config, args.train_ratio)
        report["execution_cost_sensitivity_note"] = (
            "Predeclared fee/slippage stress grid on identical history and strategy. "
            "These are assumptions, not measured live costs or executable fills; "
            "do not select a scenario from the holdout and treat it as independent evidence."
        )
    report["token_fetch_errors"] = errors
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "report_path": str(output),
        "data": report["data"],
        "in_sample_metrics": report["in_sample"]["metrics"],
        "out_of_sample_metrics": report["out_of_sample"]["metrics"],
        "price_change_diagnostics": report["price_change_diagnostics"],
        "forward_return_study": report.get("forward_return_study"),
        "warnings": report["warnings"],
        "evaluation_quality": report.get("evaluation_quality"),
        "walk_forward_aggregate": report.get("walk_forward_evaluation", {}).get("aggregate_out_of_sample"),
        "threshold_sensitivity": report.get("threshold_sensitivity"),
        "token_fetch_errors": len(errors),
        "note": "Paper simulation only; results do not guarantee future performance.",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
