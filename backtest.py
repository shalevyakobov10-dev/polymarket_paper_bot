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
    """Compare post-signal returns with all-observation returns in the same window.

    The forward point is the first recorded observation at or after the chosen
    horizon. Signals and benchmarks that lack a full in-window future point are
    omitted. This is a descriptive event study, not a portfolio simulation.
    """
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
        signal_returns: list[float] = []
        baseline_returns: list[float] = []
        signals_seen = 0
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
                while future_index < count and int(token_rows[future_index]["timestamp"]) < target_timestamp:
                    future_index += 1
                future_return = None
                if future_index < count:
                    future_return = float(token_rows[future_index]["price"]) / current_price - 1.0
                    baseline_returns.append(future_return)
                if previous_price is not None and current_price - previous_price >= threshold:
                    signals_seen += 1
                    if future_return is not None:
                        signal_returns.append(future_return)
                previous_price = current_price

        def summarize(values: list[float]) -> dict[str, Any]:
            if not values:
                return {"n": 0, "mean_return_pct": None, "median_return_pct": None, "positive_rate_pct": None}
            ordered = sorted(values)
            middle = len(ordered) // 2
            median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
            return {
                "n": len(values),
                "mean_return_pct": round(100 * sum(values) / len(values), 5),
                "median_return_pct": round(100 * median, 5),
                "positive_rate_pct": round(100 * sum(1 for value in values if value > 0) / len(values), 3),
            }

        signal_summary = summarize(signal_returns)
        baseline_summary = summarize(baseline_returns)
        excess_mean = None
        if signal_summary["mean_return_pct"] is not None and baseline_summary["mean_return_pct"] is not None:
            excess_mean = round(signal_summary["mean_return_pct"] - baseline_summary["mean_return_pct"], 5)
        output[f"{horizon}h"] = {
            "signals_detected": signals_seen,
            "signal_forward_returns": signal_summary,
            "all_observation_baseline": baseline_summary,
            "signal_minus_baseline_mean_return_pct": excess_mean,
        }
    return {
        "threshold": threshold,
        "method": "first observed price at or after horizon; only full in-window horizons; no executable fill assumptions",
        "horizons": output,
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
    for stamp, snapshot_rows in events.items():
        result = trader.process_snapshot(snapshot_rows, utc_from_timestamp(stamp), persist_state=False)
        candidates += len(result["candidates"])
        opened += len(result["opened"])
        closed += len(result["closed"])
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
            },
            "out_of_sample": {
                "candidate_signals": trial["out_of_sample"]["candidate_signals"],
                "positions_opened": trial["out_of_sample"]["positions_opened"],
                "positions_closed": trial["out_of_sample"]["positions_closed"],
                "return_pct": trial["out_of_sample"]["metrics"]["return_pct"],
                "closed_trades": trial["out_of_sample"]["metrics"]["closed_trades"],
                "win_rate_pct": trial["out_of_sample"]["metrics"]["win_rate_pct"],
            },
        })
    return results


def run_backtest(
    histories: list[dict[str, Any]],
    config: PaperConfig,
    train_ratio: float = 0.7,
) -> dict[str, Any]:
    if not 0.5 <= train_ratio < 0.9:
        raise ValueError("train_ratio must be between 0.5 and 0.9")
    cleaned = [row for row in histories if isinstance(row, dict) and number(row.get("observed_price")) is not None]
    cleaned = [row for row in cleaned if 0 < float(row["observed_price"]) < 1]
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
    report["fetched_tokens"] = len(tokens)
    report["data"]["history_fidelity_minutes"] = args.fidelity
    report["data"]["lookback_days"] = args.days
    if args.sensitivity:
        thresholds = tuple(sorted({0.005, 0.01, 0.02, 0.03, args.signal_delta}))
        report["threshold_sensitivity"] = run_threshold_sensitivity(histories, config, args.train_ratio, thresholds)
        report["sensitivity_note"] = "Exploratory threshold comparison on one sample; do not choose a threshold from holdout results and treat that same holdout as untouched. Validate selected settings on a later forward paper period."
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
        "threshold_sensitivity": report.get("threshold_sensitivity"),
        "token_fetch_errors": len(errors),
        "note": "Paper simulation only; results do not guarantee future performance.",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
