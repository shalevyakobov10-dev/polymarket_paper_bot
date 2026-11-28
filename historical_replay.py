"""Chronological, fee-aware replay of recorded event-paper entry decisions.

The replay is deliberately read-only. It consumes only decisions that were
actually entered by the event-paper engine and archived market snapshots.
Entry books must have been captured at or before the decision timestamp;
the exit book is the first eligible observation at/after the requested horizon.
This is an execution-cost research diagnostic, not a full portfolio backtest.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

from paper_orderbook_execution import simulate_buy, simulate_sell


def _utc_timestamp(value: Any, field: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _finite_number(value: Any, field: str, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field} must be numeric") from None
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return parsed


def _readonly_connection(path: str | Path) -> sqlite3.Connection:
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Database file not found: {file_path}")
    connection = sqlite3.connect(f"{file_path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _load_entered_decisions(path: str | Path, *, limit: int) -> list[dict[str, Any]]:
    connection = _readonly_connection(path)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decisions'"
        ).fetchone()
        if table is None:
            raise RuntimeError("Paper database does not contain a decisions table")
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(decisions)").fetchall()
        }
        required = {"decision_id", "observed_at", "market_id", "selected_direction", "decision"}
        missing = sorted(required - columns)
        if missing:
            raise RuntimeError(f"Paper decisions table is missing columns: {', '.join(missing)}")
        rows = connection.execute(
            """
            SELECT decision_id, observed_at, market_id, selected_direction
            FROM decisions
            WHERE UPPER(decision)='ENTERED'
            ORDER BY observed_at, decision_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _load_market_rows(
    connection: sqlite3.Connection,
    market_id: str,
    *,
    start_at: datetime,
    end_at: datetime,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT snapshot_id, observed_at, captured_at,
               yes_bid, yes_ask, no_bid, no_ask,
               yes_book_json, no_book_json
        FROM market_snapshots
        WHERE market_id=? AND captured_at>=? AND captured_at<=?
        ORDER BY captured_at, observed_at, snapshot_id
        """,
        (market_id, start_at.isoformat(), end_at.isoformat()),
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["observed_dt"] = _utc_timestamp(item["observed_at"], "snapshot observed_at")
            item["captured_dt"] = _utc_timestamp(item["captured_at"], "snapshot captured_at")
            item["yes_book"] = json.loads(item.pop("yes_book_json"))
            item["no_book"] = json.loads(item.pop("no_book_json"))
        except (TypeError, ValueError, json.JSONDecodeError):
            # A malformed archive row is unusable; it cannot become an entry or exit.
            continue
        if not isinstance(item["yes_book"], dict) or not isinstance(item["no_book"], dict):
            continue
        output.append(item)
    return output


def _decision_record(decision: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    return {
        "decision_id": decision.get("decision_id"),
        "market_id": str(decision.get("market_id") or ""),
        "direction": str(decision.get("selected_direction") or "").upper(),
        "signal_at": decision.get("observed_at"),
        "status": status,
        "reason": reason,
    }


def _summarize(trades: list[dict[str, Any]], *, decision_count: int) -> dict[str, Any]:
    closed = [row for row in trades if row.get("status") == "closed"]
    unresolved = [row for row in trades if row.get("status") == "unresolved"]
    excluded = [row for row in trades if row.get("status") == "excluded"]
    pnls = [float(row["net_pnl_usd"]) for row in closed]
    costs = sum(float(row["entry_notional_usd"]) + float(row["entry_fee_usd"]) for row in closed)
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "entered_decisions": decision_count,
        "closed_replays": len(closed),
        "unresolved_replays": len(unresolved),
        "excluded_replays": len(excluded),
        "closed_win_rate_pct": round(100.0 * len(wins) / len(closed), 4) if closed else None,
        "total_entry_notional_usd": round(sum(float(row["entry_notional_usd"]) for row in closed), 8),
        "total_fees_usd": round(sum(float(row["entry_fee_usd"]) + float(row["exit_fee_usd"]) for row in closed), 8),
        "total_net_pnl_usd": round(sum(pnls), 8),
        "net_roi_on_entry_cost_pct": round(100.0 * sum(pnls) / costs, 6) if costs > 0 else None,
        "average_net_pnl_per_closed_replay_usd": round(fmean(pnls), 8) if pnls else None,
        "average_winner_usd": round(fmean(wins), 8) if wins else None,
        "average_loser_usd": round(fmean(losses), 8) if losses else None,
        "profit_factor": round(gross_profit / gross_loss, 8) if gross_loss > 0 else None,
        "profit_factor_note": "no_closed_losses" if gross_profit > 0 and gross_loss == 0 else None,
        "average_entry_impact_bps": round(
            fmean(float(row["entry_impact_bps"]) for row in closed), 6
        ) if closed else None,
        "average_exit_impact_bps": round(
            fmean(float(row["exit_impact_bps"]) for row in closed), 6
        ) if closed else None,
        "average_realized_holding_hours": round(
            fmean(float(row["realized_holding_hours"]) for row in closed), 6
        ) if closed else None,
        "excluded_reasons": dict(Counter(str(row["reason"]) for row in excluded)),
        "unresolved_reasons": dict(Counter(str(row["reason"]) for row in unresolved)),
    }


def replay_recorded_decisions(
    paper_database_path: str | Path,
    market_history_database_path: str | Path,
    *,
    notional_usd: float = 5.0,
    holding_period_hours: float = 6.0,
    max_entry_snapshot_age_seconds: float = 300.0,
    max_exit_delay_seconds: float = 1800.0,
    fee_rate: float = 0.01,
    max_decisions: int = 100_000,
) -> dict[str, Any]:
    """Replay actual ENTERED decision records against strictly time-aligned books."""
    notional = _finite_number(notional_usd, "notional_usd", minimum=0.10, maximum=100_000.0)
    holding_hours = _finite_number(holding_period_hours, "holding_period_hours", minimum=1 / 3600, maximum=720.0)
    entry_age = _finite_number(
        max_entry_snapshot_age_seconds, "max_entry_snapshot_age_seconds",
        minimum=1.0, maximum=86_400.0,
    )
    exit_delay = _finite_number(
        max_exit_delay_seconds, "max_exit_delay_seconds",
        minimum=1.0, maximum=30 * 86_400.0,
    )
    fees = _finite_number(fee_rate, "fee_rate", minimum=0.0, maximum=0.20)
    if isinstance(max_decisions, bool) or not isinstance(max_decisions, int) or not 1 <= max_decisions <= 1_000_000:
        raise ValueError("max_decisions must be an integer between 1 and 1000000")

    decisions = _load_entered_decisions(paper_database_path, limit=max_decisions)
    history = _readonly_connection(market_history_database_path)
    try:
        table = history.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_snapshots'"
        ).fetchone()
        if table is None:
            raise RuntimeError("Market-history database does not contain market_snapshots")
        columns = {
            str(row["name"])
            for row in history.execute("PRAGMA table_info(market_snapshots)").fetchall()
        }
        required = {
            "snapshot_id", "market_id", "observed_at", "captured_at",
            "yes_bid", "yes_ask", "no_bid", "no_ask", "yes_book_json", "no_book_json",
        }
        missing = sorted(required - columns)
        if missing:
            raise RuntimeError(f"Market-history table is missing columns: {', '.join(missing)}")

        replayed: list[dict[str, Any]] = []
        for decision in decisions:
            market_id = str(decision.get("market_id") or "").strip()
            direction = str(decision.get("selected_direction") or "").upper()
            try:
                signal_at = _utc_timestamp(decision.get("observed_at"), "decision observed_at")
            except ValueError:
                replayed.append(_decision_record(decision, "excluded", "invalid_or_timezone_naive_decision_timestamp"))
                continue
            if not market_id or direction not in {"YES", "NO"}:
                replayed.append(_decision_record(decision, "excluded", "invalid_market_or_direction"))
                continue

            intended_exit = signal_at + timedelta(hours=holding_hours)
            rows = _load_market_rows(
                history, market_id,
                start_at=signal_at - timedelta(seconds=entry_age),
                end_at=intended_exit + timedelta(seconds=exit_delay),
            )
            # Both the provider observation time and local archive receipt time
            # must be no later than the decision. Future books are never entry data.
            entries = [
                row for row in rows
                if row["captured_dt"] <= signal_at
                and row["observed_dt"] <= signal_at
                and 0 <= (signal_at - row["captured_dt"]).total_seconds() <= entry_age
            ]
            if not entries:
                replayed.append(_decision_record(decision, "excluded", "no_fresh_market_snapshot_at_or_before_signal"))
                continue
            entry = max(
                entries,
                key=lambda row: (row["captured_dt"], row["observed_dt"], int(row["snapshot_id"])),
            )

            # Exit data is intentionally future data relative to the signal, but
            # must be the first observation at/after the fixed holding horizon.
            exits = [
                row for row in rows
                if row["captured_dt"] >= intended_exit
                and row["observed_dt"] >= intended_exit
                and 0 <= (row["captured_dt"] - intended_exit).total_seconds() <= exit_delay
            ]
            if not exits:
                replayed.append(_decision_record(decision, "unresolved", "no_market_snapshot_within_exit_window"))
                continue
            exit_row = min(
                exits,
                key=lambda row: (row["captured_dt"], row["observed_dt"], int(row["snapshot_id"])),
            )

            prefix = direction.lower()
            entry_book = entry[f"{prefix}_book"]
            exit_book = exit_row[f"{prefix}_book"]
            try:
                entry_fill = simulate_buy(
                    entry_book.get("asks_top5"),
                    quote_budget=notional,
                )
            except (AttributeError, TypeError, ValueError):
                replayed.append(_decision_record(decision, "excluded", "entry_order_book_unusable"))
                continue
            if abs(float(entry_fill["best_price"]) - float(entry[f"{prefix}_ask"])) > 1e-8:
                replayed.append(_decision_record(decision, "excluded", "entry_best_ask_mismatch"))
                continue
            if entry_fill["complete"] is not True:
                replayed.append(_decision_record(decision, "excluded", "insufficient_visible_entry_ask_depth"))
                continue

            try:
                exit_fill = simulate_sell(
                    exit_book.get("bids_top5"),
                    shares=float(entry_fill["shares_filled"]),
                )
            except (AttributeError, TypeError, ValueError):
                replayed.append(_decision_record(decision, "unresolved", "exit_order_book_unusable"))
                continue
            if abs(float(exit_fill["best_price"]) - float(exit_row[f"{prefix}_bid"])) > 1e-8:
                replayed.append(_decision_record(decision, "unresolved", "exit_best_bid_mismatch"))
                continue
            if exit_fill["complete"] is not True:
                replayed.append(_decision_record(decision, "unresolved", "insufficient_visible_exit_bid_depth"))
                continue

            entry_cost = float(entry_fill["quote_filled"])
            entry_fee = entry_cost * fees
            exit_proceeds = float(exit_fill["quote_proceeds"])
            exit_fee = exit_proceeds * fees
            net_pnl = exit_proceeds - exit_fee - entry_cost - entry_fee
            total_entry_cost = entry_cost + entry_fee
            realized_hours = (
                exit_row["captured_dt"] - entry["captured_dt"]
            ).total_seconds() / 3600.0
            replayed.append({
                "decision_id": decision.get("decision_id"),
                "market_id": market_id,
                "direction": direction,
                "status": "closed",
                "signal_at": signal_at.isoformat(),
                "entry_captured_at": entry["captured_dt"].isoformat(),
                "entry_observed_at": entry["observed_dt"].isoformat(),
                "entry_snapshot_age_seconds": round(
                    (signal_at - entry["captured_dt"]).total_seconds(), 6
                ),
                "intended_exit_at": intended_exit.isoformat(),
                "exit_captured_at": exit_row["captured_dt"].isoformat(),
                "exit_observed_at": exit_row["observed_dt"].isoformat(),
                "exit_delay_seconds": round(
                    (exit_row["captured_dt"] - intended_exit).total_seconds(), 6
                ),
                "realized_holding_hours": round(realized_hours, 8),
                "entry_notional_usd": round(entry_cost, 8),
                "entry_fee_usd": round(entry_fee, 8),
                "exit_proceeds_usd": round(exit_proceeds, 8),
                "exit_fee_usd": round(exit_fee, 8),
                "net_pnl_usd": round(net_pnl, 8),
                "net_roi_pct": round(100.0 * net_pnl / total_entry_cost, 8) if total_entry_cost > 0 else None,
                "entry_average_price": round(float(entry_fill["average_price"]), 8),
                "exit_average_price": round(float(exit_fill["average_price"]), 8),
                "entry_impact_bps": round(float(entry_fill["price_impact_bps"] or 0.0), 8),
                "exit_impact_bps": round(float(exit_fill["price_impact_bps"] or 0.0), 8),
                "entry_levels_used": int(entry_fill["levels_used"]),
                "exit_levels_used": int(exit_fill["levels_used"]),
                "execution": "simulated_only",
            })
    finally:
        history.close()

    report = {
        "mode": "CHRONOLOGICAL_EVENT_PAPER_MARKET_REPLAY_V1",
        "status": "completed" if decisions else "insufficient_data",
        "parameters": {
            "fixed_entry_notional_usd": notional,
            "holding_period_hours": holding_hours,
            "max_entry_snapshot_age_seconds": entry_age,
            "max_exit_delay_seconds": exit_delay,
            "fee_rate_per_side": fees,
            "max_decisions": max_decisions,
            "entry_selection": "latest_snapshot_with_captured_at_and_observed_at_at_or_before_decision",
            "exit_selection": "first_snapshot_with_captured_at_and_observed_at_at_or_after_holding_horizon",
        },
        "metrics": _summarize(replayed, decision_count=len(decisions)),
        "trades": replayed,
        "limitations": [
            "Uses only decisions recorded as ENTERED by the running paper engine; it does not regenerate historical news signals.",
            "Fixed notional is a benchmark and does not reproduce historical dynamic sizing, portfolio capacity, overlapping exposure, or compounding.",
            "The exit is the first archived book at or after the requested horizon, not a stop-loss/take-profit path or an exchange-confirmed fill.",
            "Top-five visible depth, queue priority, hidden liquidity, latency, and unrecorded intraperiod book changes are not modeled.",
            "Only fully simulated entry and exit fills are included in closed-trade metrics; unresolved and excluded decisions are reported separately.",
            "A replay result is not proof of a repeatable trading edge or eligibility for real-money execution.",
        ],
        "out_of_sample_validated": False,
        "live_execution_eligible": False,
        "real_money_execution": "not_implemented",
        "research_only": True,
        "execution": "none",
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay recorded Paper entry decisions against archived CLOB books.")
    parser.add_argument("--paper-db", required=True, help="Path to the offline event-paper SQLite database")
    parser.add_argument("--market-history-db", required=True, help="Path to the append-only market-history SQLite database")
    parser.add_argument("--notional-usd", type=float, default=5.0)
    parser.add_argument("--holding-hours", type=float, default=6.0)
    parser.add_argument("--max-entry-age-seconds", type=float, default=300.0)
    parser.add_argument("--max-exit-delay-seconds", type=float, default=1800.0)
    parser.add_argument("--fee-rate", type=float, default=0.01)
    parser.add_argument("--max-decisions", type=int, default=100_000)
    parser.add_argument("--output", help="Optional JSON report path; stdout is always populated")
    args = parser.parse_args()

    report = replay_recorded_decisions(
        args.paper_db,
        args.market_history_db,
        notional_usd=args.notional_usd,
        holding_period_hours=args.holding_hours,
        max_entry_snapshot_age_seconds=args.max_entry_age_seconds,
        max_exit_delay_seconds=args.max_exit_delay_seconds,
        fee_rate=args.fee_rate,
        max_decisions=args.max_decisions,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    print(rendered)
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
