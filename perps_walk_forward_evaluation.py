"""Chronological, fixed-policy walk-forward diagnostics for Polymarket Perps.

Each test fold starts a fresh Paper ledger and receives only historical market
and funding observations captured by each simulated decision time. Earlier
snapshots warm up the momentum signal but cannot open a position. The strategy
configuration is frozen across folds: this tool does not optimize parameters.
Even a positive consistency result is exploratory evidence, never live-trading
approval or a claim of out-of-sample validation.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from perps_historical_replay import replay_perps_history, _parse_aware
from perps_market_history_store import PerpsMarketHistoryStore


ZERO = Decimal("0")
ONE = Decimal("1")


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} must be a finite decimal") from None
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return parsed


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def evaluate_perps_walk_forward(
    history_database_path: str | Path,
    *,
    start_received_at: str | datetime | None = None,
    end_received_at: str | datetime | None = None,
    folds: int = 3,
    warmup_fraction: float = 0.30,
    max_snapshots: int = 100_000,
    minimum_closed_trades_per_fold: int = 20,
    minimum_total_closed_trades: int = 60,
    maximum_peak_drawdown_pct: Any = "10",
    engine_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate one frozen Perps policy over contiguous, non-overlapping test folds.

    Fold boundaries are selected from distinct capture timestamps, not row
    indices, so instruments captured in the same polling cycle remain together.
    Every fold has its own fresh Paper ledger; prior rows only build its
    point-in-time lookback history. Positions still open at fold end are
    reported separately and never force-closed to manufacture realized PnL.
    """
    source_path = Path(history_database_path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError("Perps market-history database does not exist")
    fold_count = _bounded_int(folds, "folds", 2, 6)
    _bounded_int(max_snapshots, "max_snapshots", 1, 100_000)
    _bounded_int(
        minimum_closed_trades_per_fold,
        "minimum_closed_trades_per_fold",
        1,
        1000,
    )
    _bounded_int(
        minimum_total_closed_trades,
        "minimum_total_closed_trades",
        1,
        10_000,
    )
    if (
        isinstance(warmup_fraction, bool)
        or not isinstance(warmup_fraction, (int, float))
        or not 0.15 <= float(warmup_fraction) <= 0.70
    ):
        raise ValueError("warmup_fraction must be between 0.15 and 0.70")
    max_drawdown = _decimal(maximum_peak_drawdown_pct, "maximum_peak_drawdown_pct")
    if not ZERO < max_drawdown < Decimal("100"):
        raise ValueError("maximum_peak_drawdown_pct must be between 0 and 100")
    if engine_options is not None and not isinstance(engine_options, dict):
        raise ValueError("engine_options must be an object")

    start = _parse_aware(start_received_at, "start_received_at") if start_received_at is not None else None
    end = _parse_aware(end_received_at, "end_received_at") if end_received_at is not None else None
    if start is not None and end is not None and start > end:
        raise ValueError("start_received_at must not exceed end_received_at")

    store = PerpsMarketHistoryStore(source_path)
    try:
        rows = store.snapshots(
            start_received_at=start,
            end_received_at=end,
            limit=max_snapshots,
        )
        if not rows:
            raise ValueError("no archived Perps snapshots exist in the requested walk-forward window")

        capture_times = sorted({
            _parse_aware(row["received_at"], "received_at")
            for row in rows
        })
        if len(capture_times) < fold_count + 3:
            raise ValueError("not enough distinct capture timestamps for warmup and test folds")

        warmup_count = max(2, int(len(capture_times) * float(warmup_fraction)))
        if len(capture_times) - warmup_count < fold_count:
            raise ValueError("not enough post-warmup capture timestamps for all test folds")
        test_time_count = len(capture_times) - warmup_count
        boundaries: list[tuple[datetime, datetime]] = []
        for index in range(fold_count):
            left = warmup_count + (test_time_count * index // fold_count)
            right_exclusive = warmup_count + (test_time_count * (index + 1) // fold_count)
            if right_exclusive <= left:
                raise ValueError("walk-forward fold has an empty test interval")
            boundaries.append((capture_times[left], capture_times[right_exclusive - 1]))

        first_capture = capture_times[0]
        last_capture = capture_times[-1]
        fold_reports: list[dict[str, Any]] = []
        total_closed = 0
        total_entries = 0
        total_winning_net = ZERO
        total_losing_net_abs = ZERO
        total_net_pnl = ZERO
        all_folds_passed = True

        for fold_index, (test_start, test_end) in enumerate(boundaries, start=1):
            warmup_rows = sum(
                1 for row in rows
                if _parse_aware(row["received_at"], "received_at") < test_start
            )
            test_rows = sum(
                1 for row in rows
                if test_start <= _parse_aware(row["received_at"], "received_at") <= test_end
            )
            # replay_perps_history creates a fresh temporary ledger. The same
            # fixed configuration is used for every fold, and all pre-test
            # snapshots are visible only as history, never as trade decisions.
            report = replay_perps_history(
                source_path,
                start_received_at=first_capture,
                end_received_at=test_end,
                max_snapshots=max_snapshots,
                engine_options=engine_options,
                entry_start_received_at=test_start,
            )
            performance = report.get("performance")
            portfolio = report.get("portfolio")
            if not isinstance(performance, dict) or not isinstance(portfolio, dict):
                raise ValueError("historical replay returned an incomplete fold report")

            closed = _bounded_int(
                performance.get("closed_trades"),
                "fold.closed_trades",
                0,
                1_000_000,
            )
            entries = _bounded_int(
                report.get("paper_entry_events"),
                "fold.paper_entry_events",
                0,
                1_000_000,
            )
            net_pnl = _decimal(
                performance.get("net_closed_pnl_after_fees_and_funding", "0"),
                "fold.net_closed_pnl_after_fees_and_funding",
            )
            winning_net = _decimal(performance.get("winning_net_pnl", "0"), "fold.winning_net_pnl")
            losing_net_abs = _decimal(
                performance.get("losing_net_pnl_abs", "0"),
                "fold.losing_net_pnl_abs",
            )
            profit_factor_raw = performance.get("profit_factor")
            profit_factor = (
                _decimal(profit_factor_raw, "fold.profit_factor")
                if profit_factor_raw is not None else None
            )
            drawdown = _decimal(
                portfolio.get("peak_drawdown_pct", "0"),
                "fold.peak_drawdown_pct",
            )
            open_positions = _bounded_int(
                portfolio.get("open_positions", 0),
                "fold.open_positions",
                0,
                100_000,
            )
            pending_exits = _bounded_int(
                portfolio.get("pending_exit_positions", 0),
                "fold.pending_exit_positions",
                0,
                100_000,
            )

            reasons: list[str] = []
            if closed < minimum_closed_trades_per_fold:
                reasons.append("insufficient_closed_trades")
            if net_pnl <= ZERO:
                reasons.append("nonpositive_net_pnl_after_fees_and_funding")
            if profit_factor is None or profit_factor <= ONE:
                reasons.append("profit_factor_not_above_one")
            if drawdown > max_drawdown:
                reasons.append("peak_drawdown_above_policy_limit")
            if open_positions or pending_exits:
                reasons.append("positions_unresolved_at_test_window_end")
            fold_passed = not reasons
            all_folds_passed = all_folds_passed and fold_passed

            fold_reports.append({
                "fold": fold_index,
                "warmup_start": first_capture.isoformat(),
                "warmup_snapshots": warmup_rows,
                "test_start": test_start.isoformat(),
                "test_end": test_end.isoformat(),
                "test_snapshots": test_rows,
                "snapshots_replayed_including_warmup": report.get("snapshots_replayed"),
                "paper_entry_events": entries,
                "closed_trades": closed,
                "open_positions_at_test_end": open_positions,
                "pending_exit_positions_at_test_end": pending_exits,
                "net_closed_pnl_after_fees_and_funding": str(net_pnl),
                "net_expectancy_per_closed_trade": performance.get("net_expectancy_per_closed_trade"),
                "profit_factor": str(profit_factor) if profit_factor is not None else None,
                "peak_drawdown_pct": str(drawdown),
                "fold_consistency_passed": fold_passed,
                "blocked_reasons": reasons,
                "fresh_paper_ledger_per_fold": True,
                "strategy_parameters_changed": False,
            })
            total_closed += closed
            total_entries += entries
            total_net_pnl += net_pnl
            total_winning_net += winning_net
            total_losing_net_abs += losing_net_abs

        aggregate_pf = (
            total_winning_net / total_losing_net_abs
            if total_losing_net_abs > ZERO else None
        )
        minimum_sample_passed = (
            total_closed >= minimum_total_closed_trades
            and all(
                fold["closed_trades"] >= minimum_closed_trades_per_fold
                for fold in fold_reports
            )
        )
        consistency_passed = all_folds_passed and minimum_sample_passed
        if not minimum_sample_passed:
            validation_state = "insufficient_sample"
        elif consistency_passed:
            validation_state = "walk_forward_consistency_passed_exploratory"
        else:
            validation_state = "walk_forward_consistency_failed"

        return {
            "mode": "POLYMARKET_PERPS_FROZEN_POLICY_WALK_FORWARD_DIAGNOSTIC",
            "validation_state": validation_state,
            "source_database": source_path.name,
            "first_capture_at": first_capture.isoformat(),
            "last_capture_at": last_capture.isoformat(),
            "distinct_capture_timestamps": len(capture_times),
            "snapshots_in_requested_window": len(rows),
            "fold_count": fold_count,
            "warmup_fraction": str(Decimal(str(warmup_fraction))),
            "minimum_closed_trades_per_fold": minimum_closed_trades_per_fold,
            "minimum_total_closed_trades": minimum_total_closed_trades,
            "maximum_peak_drawdown_pct": str(max_drawdown),
            "entry_policy_frozen_across_folds": True,
            "parameter_tuning_performed": False,
            "folds_use_fresh_paper_ledgers": True,
            "aggregate": {
                "paper_entry_events": total_entries,
                "closed_trades": total_closed,
                "net_closed_pnl_after_fees_and_funding": str(total_net_pnl),
                "winning_net_pnl": str(total_winning_net),
                "losing_net_pnl_abs": str(total_losing_net_abs),
                "profit_factor": str(aggregate_pf) if aggregate_pf is not None else None,
                "minimum_sample_passed": minimum_sample_passed,
                "all_folds_consistent": all_folds_passed,
            },
            "fold_reports": fold_reports,
            "point_in_time_protection": {
                "chronological_non_overlapping_test_windows": True,
                "same_capture_timestamp_never_split_across_folds": True,
                "pre_test_snapshots_used_only_for_signal_warmup": True,
                "pre_test_entries_suppressed": True,
                "market_history_as_of_received_at": True,
                "funding_history_as_of_received_at": True,
                "exchange_timestamps_must_not_exceed_decision_time": True,
                "fresh_paper_ledger_for_each_fold": True,
            },
            "interpretation": (
                "Exploratory chronological consistency diagnostic of a frozen policy. "
                "Closed-trade PnL includes modeled taker fees and published funding; "
                "positions remaining open at a fold boundary are reported, not force-closed. "
                "A consistency pass is not independent statistical proof or a profitability guarantee."
            ),
            "research_only": True,
            "walk_forward_consistency_passed": consistency_passed,
            "out_of_sample_validated": False,
            "live_execution_eligible": False,
            "live_execution_enabled": False,
            "order_submission_allowed": False,
            "execution": "none",
        }
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run frozen-policy chronological walk-forward diagnostics on archived Perps data."
    )
    parser.add_argument(
        "--database",
        default=os.getenv("PERPS_MARKET_HISTORY_DB", "data/perps_market_history.sqlite3"),
        help="Path to the archived public Perps market-history SQLite database.",
    )
    parser.add_argument("--from", dest="start_received_at", help="Inclusive ISO-8601 capture-time start.")
    parser.add_argument("--to", dest="end_received_at", help="Inclusive ISO-8601 capture-time end.")
    parser.add_argument("--folds", type=int, default=3, help="Contiguous future test windows (2–6).")
    parser.add_argument("--warmup-fraction", type=float, default=0.30, help="Initial history-only fraction (0.15–0.70).")
    parser.add_argument("--max-snapshots", type=int, default=100000, help="Maximum archived rows (1–100000).")
    parser.add_argument("--minimum-closed-trades-per-fold", type=int, default=20)
    parser.add_argument("--minimum-total-closed-trades", type=int, default=60)
    parser.add_argument("--maximum-peak-drawdown-pct", default="10")
    args = parser.parse_args(argv)
    try:
        report = evaluate_perps_walk_forward(
            args.database,
            start_received_at=args.start_received_at,
            end_received_at=args.end_received_at,
            folds=args.folds,
            warmup_fraction=args.warmup_fraction,
            max_snapshots=args.max_snapshots,
            minimum_closed_trades_per_fold=args.minimum_closed_trades_per_fold,
            minimum_total_closed_trades=args.minimum_total_closed_trades,
            maximum_peak_drawdown_pct=args.maximum_peak_drawdown_pct,
        )
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, f"Perps walk-forward evaluation failed safely: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
