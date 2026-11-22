"""Resolution-aligned, cost-aware evaluation of event-paper entries.

This report asks a narrow counterfactual question: if each recorded event-paper
entry had been held to its market's definitive binary resolution, what would
the entry have returned after its recorded entry fee and the visible ask-side
depth cost? It is not the same as the paper engine's actual stop/time exits.

Inputs are opened in read-only mode. Training labels in each walk-forward fold
must have resolved before that fold's first decision; later labels are purged
from training. No result authorizes live execution.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

from paper_orderbook_execution import simulate_buy

_ALLOWED_RESOLUTION_STATUSES = {
    "resolved",
    "settled",
    "legacy_exact_binary_closed",
}


def _utc(value: Any, field: str) -> datetime:
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


def _number(value: Any, field: str, *, low: float, high: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field} must be numeric") from None
    if not math.isfinite(parsed) or not low <= parsed <= high:
        raise ValueError(f"{field} must be between {low} and {high}")
    return parsed


def _readonly_db(path: str | Path) -> sqlite3.Connection:
    database = Path(path).expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError(f"Database file not found: {database}")
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _require_columns(
    connection: sqlite3.Connection,
    table: str,
    required: set[str],
) -> None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if exists is None:
        raise RuntimeError(f"Database does not contain required table: {table}")
    columns = {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError(f"Table {table} is missing columns: {', '.join(missing)}")


def _resolution_paths(path: str | Path, backup_count: int) -> list[Path]:
    base = Path(path).expanduser()
    # Match ResolutionCollector's bounded JSONL rotation convention.
    return [
        base.with_name(f"{base.name}.{index}")
        for index in range(backup_count, 0, -1)
    ] + [base]


def load_definitive_resolutions(
    path: str | Path,
    *,
    backup_count: int = 3,
) -> tuple[dict[str, dict[str, Any]], dict[str, str], dict[str, int]]:
    """Load retained exact-binary settlement records; conflicting labels are withheld."""
    if isinstance(backup_count, bool) or not isinstance(backup_count, int) or not 0 <= backup_count <= 10:
        raise ValueError("backup_count must be an integer between 0 and 10")

    candidates: dict[str, list[dict[str, Any]]] = {}
    diagnostics = {
        "resolution_files_found": 0,
        "resolution_rows_read": 0,
        "invalid_resolution_rows": 0,
        "conflicting_resolution_markets": 0,
    }
    for candidate_path in _resolution_paths(path, backup_count):
        if not candidate_path.is_file():
            continue
        diagnostics["resolution_files_found"] += 1
        try:
            with candidate_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError("resolution row must be an object")
                        market_id = str(row.get("market_id") or "").strip()
                        outcome = row.get("resolved_yes")
                        if (
                            not market_id
                            or isinstance(outcome, bool)
                            or not isinstance(outcome, int)
                            or outcome not in (0, 1)
                        ):
                            raise ValueError("market ID and integer binary outcome are required")
                        resolution_status = str(row.get("resolution_status") or "").strip().casefold()
                        if resolution_status not in _ALLOWED_RESOLUTION_STATUSES:
                            raise ValueError("resolution status is not definitive")
                        resolved_at = _utc(row.get("resolved_at"), "resolved_at")
                        if row.get("source") != "public_gamma_resolved_market":
                            raise ValueError("resolution source is not the supported Gamma collector")
                        normalized = {
                            "market_id": market_id,
                            "resolved_yes": outcome,
                            "resolved_at_dt": resolved_at,
                            "resolved_at": resolved_at.isoformat(),
                            "resolution_status": resolution_status,
                            "resolution_source": str(row.get("resolution_source") or ""),
                        }
                        candidates.setdefault(market_id, []).append(normalized)
                        diagnostics["resolution_rows_read"] += 1
                    except (ValueError, TypeError, json.JSONDecodeError):
                        diagnostics["invalid_resolution_rows"] += 1
        except OSError:
            diagnostics["invalid_resolution_rows"] += 1

    accepted: dict[str, dict[str, Any]] = {}
    withheld: dict[str, str] = {}
    for market_id, rows in candidates.items():
        outcomes = {int(row["resolved_yes"]) for row in rows}
        if len(outcomes) != 1:
            withheld[market_id] = "conflicting_resolution_labels_across_retained_records"
            diagnostics["conflicting_resolution_markets"] += 1
            continue
        accepted[market_id] = max(rows, key=lambda row: row["resolved_at_dt"])
    return accepted, withheld, diagnostics


def _load_decision_trade_pairs(path: str | Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    connection = _readonly_db(path)
    try:
        _require_columns(
            connection,
            "decisions",
            {"decision_id", "observed_at", "market_id", "selected_direction", "decision"},
        )
        _require_columns(
            connection,
            "trades",
            {
                "trade_id", "market_id", "direction", "opened_at", "entry_price",
                "shares", "notional", "entry_fee", "signal_strength", "entry_threshold",
                "status", "closed_at", "pnl",
            },
        )
        decisions = connection.execute(
            """
            SELECT decision_id, observed_at, market_id, selected_direction
            FROM decisions
            WHERE UPPER(decision)='ENTERED'
            ORDER BY observed_at, decision_id
            """
        ).fetchall()
        trades = connection.execute(
            """
            SELECT trade_id, market_id, question, direction, opened_at, entry_price,
                   shares, notional, entry_fee, signal_strength, entry_threshold,
                   status, closed_at, pnl
            FROM trades
            ORDER BY opened_at, trade_id
            """
        ).fetchall()
        diagnostics = {
            "entered_decisions": len(decisions),
            "paper_trade_rows": len(trades),
            "unmatched_entered_decisions": 0,
            "ambiguous_decision_trade_matches": 0,
        }

        trade_index: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in trades:
            item = dict(row)
            try:
                opened = _utc(item["opened_at"], "trade opened_at")
            except ValueError:
                continue
            item["_opened_dt"] = opened
            key = (
                str(item.get("market_id") or "").strip(),
                str(item.get("direction") or "").upper(),
                opened.isoformat(),
            )
            trade_index.setdefault(key, []).append(item)

        matched: list[dict[str, Any]] = []
        used_trade_ids: set[int] = set()
        for raw in decisions:
            decision = dict(raw)
            try:
                decided_at = _utc(decision.get("observed_at"), "decision observed_at")
            except ValueError:
                decision["_invalid_reason"] = "invalid_or_timezone_naive_decision_timestamp"
                matched.append(decision)
                continue
            market_id = str(decision.get("market_id") or "").strip()
            direction = str(decision.get("selected_direction") or "").upper()
            key = (market_id, direction, decided_at.isoformat())
            possible = trade_index.get(key, [])
            if len(possible) != 1:
                if len(possible) > 1:
                    diagnostics["ambiguous_decision_trade_matches"] += 1
                    decision["_invalid_reason"] = "ambiguous_paper_trade_ledger_match"
                else:
                    diagnostics["unmatched_entered_decisions"] += 1
                    decision["_invalid_reason"] = "entered_decision_without_trade_ledger_match"
                matched.append(decision)
                continue
            trade = possible[0]
            trade_id = int(trade["trade_id"])
            if trade_id in used_trade_ids:
                decision["_invalid_reason"] = "paper_trade_matched_to_multiple_decisions"
                matched.append(decision)
                continue
            used_trade_ids.add(trade_id)
            matched.append({**decision, "_trade": trade, "_decision_dt": decided_at})
        return matched, diagnostics
    finally:
        connection.close()


def _entry_snapshot(
    connection: sqlite3.Connection,
    market_id: str,
    decided_at: datetime,
    *,
    max_age_seconds: float,
) -> tuple[dict[str, Any] | None, str | None]:
    rows = connection.execute(
        """
        SELECT snapshot_id, observed_at, captured_at, yes_ask, no_ask,
               yes_book_json, no_book_json
        FROM market_snapshots
        WHERE market_id=? AND captured_at>=? AND captured_at<=?
        ORDER BY captured_at DESC, snapshot_id DESC
        LIMIT 200
        """,
        (
            market_id,
            (decided_at - timedelta(seconds=max_age_seconds)).isoformat(),
            decided_at.isoformat(),
        ),
    ).fetchall()
    usable = []
    for raw in rows:
        row = dict(raw)
        try:
            observed = _utc(row["observed_at"], "snapshot observed_at")
            captured = _utc(row["captured_at"], "snapshot captured_at")
            yes_book = json.loads(row.pop("yes_book_json"))
            no_book = json.loads(row.pop("no_book_json"))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if (
            observed > decided_at
            or captured > decided_at
            or (decided_at - captured).total_seconds() > max_age_seconds
            or not isinstance(yes_book, dict)
            or not isinstance(no_book, dict)
        ):
            continue
        row.update({
            "_observed_dt": observed,
            "_captured_dt": captured,
            "yes_book": yes_book,
            "no_book": no_book,
        })
        usable.append(row)
    if not usable:
        return None, "no_fresh_pre_decision_market_snapshot"
    return max(
        usable,
        key=lambda row: (
            row["_captured_dt"],
            row["_observed_dt"],
            int(row["snapshot_id"]),
        ),
    ), None


def _validate_and_score_entry(
    decision: dict[str, Any],
    resolution: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    settlement_fee_rate: float,
) -> tuple[dict[str, Any] | None, str | None]:
    trade = decision.get("_trade")
    if not isinstance(trade, dict):
        return None, "paper_trade_ledger_match_missing"

    market_id = str(decision.get("market_id") or "").strip()
    direction = str(decision.get("selected_direction") or "").upper()
    if direction not in {"YES", "NO"}:
        return None, "invalid_selected_direction"
    decided_at = decision["_decision_dt"]
    if resolution["resolved_at_dt"] <= decided_at:
        return None, "market_resolved_before_or_at_entry_decision"

    try:
        notional = _number(trade.get("notional"), "trade notional", low=0.01, high=1_000_000.0)
        shares = _number(trade.get("shares"), "trade shares", low=1e-9, high=10_000_000.0)
        entry_fee = _number(trade.get("entry_fee"), "entry fee", low=0.0, high=200_000.0)
        ledger_entry_price = _number(trade.get("entry_price"), "ledger entry price", low=0.0, high=1.0)
        signal_strength = _number(trade.get("signal_strength"), "signal strength", low=0.0, high=1.0)
        entry_threshold = _number(trade.get("entry_threshold"), "entry threshold", low=0.0, high=1.0)
    except ValueError:
        return None, "invalid_paper_trade_ledger_economics"

    suffix = "yes" if direction == "YES" else "no"
    book = snapshot.get(f"{suffix}_book")
    try:
        asks = book.get("asks_top5") if isinstance(book, dict) else None
        fill = simulate_buy(asks, quote_budget=notional)
    except (AttributeError, TypeError, ValueError):
        return None, "entry_ask_book_unusable"
    best_ask = float(snapshot[f"{suffix}_ask"])
    if abs(float(fill["best_price"]) - best_ask) > 1e-8:
        return None, "entry_best_ask_does_not_match_archived_book"
    if fill["complete"] is not True:
        return None, "insufficient_archived_visible_entry_depth"
    if abs(float(fill["average_price"]) - ledger_entry_price) > 1e-5:
        return None, "replayed_entry_price_does_not_match_paper_ledger"
    if abs(float(fill["shares_filled"]) - shares) > max(1e-5, shares * 1e-5):
        return None, "replayed_entry_shares_do_not_match_paper_ledger"

    resolved_yes = int(resolution["resolved_yes"])
    winning_direction = "YES" if resolved_yes == 1 else "NO"
    gross_settlement_payout = shares if direction == winning_direction else 0.0
    settlement_fee = gross_settlement_payout * settlement_fee_rate
    net_settlement_payout = gross_settlement_payout - settlement_fee
    entry_cost = notional + entry_fee
    net_pnl = net_settlement_payout - entry_cost
    net_roi_pct = 100.0 * net_pnl / entry_cost if entry_cost > 0 else None
    try:
        actual_closed_at = (
            _utc(trade["closed_at"], "trade closed_at").isoformat()
            if trade.get("closed_at") else None
        )
    except ValueError:
        actual_closed_at = None
    actual_pnl = trade.get("pnl")
    try:
        actual_pnl = float(actual_pnl) if actual_pnl is not None and math.isfinite(float(actual_pnl)) else None
    except (TypeError, ValueError, OverflowError):
        actual_pnl = None

    return {
        "decision_id": int(decision["decision_id"]),
        "trade_id": int(trade["trade_id"]),
        "market_id": market_id,
        "direction": direction,
        "question": str(trade.get("question") or "")[:500],
        "decision_at": decided_at.isoformat(),
        "market_resolved_at": resolution["resolved_at"],
        "resolution_source": resolution.get("resolution_source") or "public_gamma_resolved_market",
        "resolution_status": resolution["resolution_status"],
        "resolved_yes": resolved_yes,
        "prediction_correct": direction == winning_direction,
        "paper_trade_status": str(trade.get("status") or ""),
        "paper_trade_actual_closed_at": actual_closed_at,
        "paper_trade_actual_net_pnl_usd": actual_pnl,
        "signal_strength": signal_strength,
        "entry_threshold": entry_threshold,
        "threshold_multiplier_ratio": signal_strength / entry_threshold if entry_threshold > 0 else None,
        "entry_captured_at": snapshot["_captured_dt"].isoformat(),
        "entry_observed_at": snapshot["_observed_dt"].isoformat(),
        "entry_snapshot_age_seconds": round(
            (decided_at - snapshot["_captured_dt"]).total_seconds(), 6
        ),
        "entry_notional_usd": round(notional, 8),
        "entry_fee_usd": round(entry_fee, 8),
        "entry_fee_rate_observed": round(entry_fee / notional, 8) if notional else None,
        "entry_average_price": round(float(fill["average_price"]), 8),
        "entry_best_ask": round(best_ask, 8),
        "entry_price_impact_bps": round(float(fill["price_impact_bps"] or 0.0), 8),
        "shares": round(shares, 8),
        "gross_settlement_payout_usd": round(gross_settlement_payout, 8),
        "settlement_fee_usd": round(settlement_fee, 8),
        "net_settlement_payout_usd": round(net_settlement_payout, 8),
        "counterfactual_net_pnl_usd": round(net_pnl, 8),
        "counterfactual_net_roi_pct": round(net_roi_pct, 8) if net_roi_pct is not None else None,
        "counterfactual_result": "win" if direction == winning_direction else "loss",
        "entry_execution_cost_model": "recorded_quote_notional_and_replayed_top5_ask_vwap",
        "execution": "counterfactual_settlement_only",
    }, None


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    actual_rows = [
        row for row in rows
        if str(row.get("paper_trade_status") or "").upper() == "CLOSED"
        and isinstance(row.get("paper_trade_actual_net_pnl_usd"), (int, float))
        and math.isfinite(float(row["paper_trade_actual_net_pnl_usd"]))
    ]
    actual_pnl = [float(row["paper_trade_actual_net_pnl_usd"]) for row in actual_rows]
    actual_wins = sum(1 for value in actual_pnl if value > 0)
    actual_losses = sum(1 for value in actual_pnl if value < 0)
    actual_metrics = {
        "actual_paper_closed_trade_count": len(actual_rows),
        "actual_paper_realized_net_pnl_usd": round(sum(actual_pnl), 8),
        "actual_paper_realized_wins": actual_wins,
        "actual_paper_realized_losses": actual_losses,
        "actual_paper_realized_win_rate_pct": (
            round(100.0 * actual_wins / len(actual_rows), 4) if actual_rows else None
        ),
        "actual_paper_average_realized_pnl_usd": round(mean(actual_pnl), 8) if actual_rows else None,
    }
    if not rows:
        return {
            "n": 0, "wins": 0, "losses": 0, "win_rate_pct": None,
            "total_entry_cost_usd": 0.0, "total_net_pnl_usd": 0.0,
            "net_roi_pct": None, "average_net_pnl_usd": None,
            "profit_factor": None, "average_entry_impact_bps": None,
            **actual_metrics,
        }
    pnl = [float(row["counterfactual_net_pnl_usd"]) for row in rows]
    costs = [float(row["entry_notional_usd"]) + float(row["entry_fee_usd"]) for row in rows]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    invested = sum(costs)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "n": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "break_even": sum(1 for value in pnl if value == 0),
        "win_rate_pct": round(100.0 * len(wins) / len(rows), 4),
        "total_entry_cost_usd": round(invested, 8),
        "total_entry_notional_usd": round(sum(float(row["entry_notional_usd"]) for row in rows), 8),
        "total_entry_fees_usd": round(sum(float(row["entry_fee_usd"]) for row in rows), 8),
        "total_settlement_fees_usd": round(sum(float(row["settlement_fee_usd"]) for row in rows), 8),
        "total_net_pnl_usd": round(sum(pnl), 8),
        "net_roi_pct": round(100.0 * sum(pnl) / invested, 8) if invested > 0 else None,
        "average_net_pnl_usd": round(mean(pnl), 8),
        "average_net_roi_per_trade_pct": round(mean(float(row["counterfactual_net_roi_pct"]) for row in rows), 8),
        "profit_factor": round(gross_profit / gross_loss, 8) if gross_loss > 0 else None,
        "average_entry_impact_bps": round(mean(float(row["entry_price_impact_bps"]) for row in rows), 8),
        **actual_metrics,
    }


def _apply_multiplier(rows: list[dict[str, Any]], multiplier: float) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if float(row["signal_strength"]) + 1e-12
        >= float(row["entry_threshold"]) * multiplier
    ]


def chronological_resolution_walk_forward(
    samples: list[dict[str, Any]],
    *,
    train_fraction: float = 0.5,
    folds: int = 3,
    minimum_train_markets: int = 20,
    minimum_test_markets: int = 10,
) -> dict[str, Any]:
    """Expanding-window holdout; only labels known by each test start can train it."""
    if not 0.4 <= train_fraction <= 0.8:
        raise ValueError("train_fraction must be between 0.4 and 0.8")
    if isinstance(folds, bool) or not isinstance(folds, int) or not 2 <= folds <= 5:
        raise ValueError("folds must be an integer between 2 and 5")
    for name, value in (
        ("minimum_train_markets", minimum_train_markets),
        ("minimum_test_markets", minimum_test_markets),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 2:
            raise ValueError(f"{name} must be an integer >= 2")

    ordered = sorted(samples, key=lambda row: (row["_decision_dt"], row["market_id"]))
    base = {
        "method": "expanding_window_resolution_time_purged_walk_forward_v1",
        "unique_markets": len(ordered),
        "train_fraction": train_fraction,
        "folds_requested": folds,
        "minimum_train_markets": minimum_train_markets,
        "minimum_test_markets": minimum_test_markets,
        "folds": [],
        "folds_scored": 0,
        "folds_passed": 0,
        "walk_forward_consistency_gate_passed": False,
        "out_of_sample_validated": False,
        "live_execution_eligible": False,
        "real_money_execution": "not_implemented",
        "research_only": True,
        "execution": "none",
    }
    minimum_total = minimum_train_markets + folds * minimum_test_markets
    if len(ordered) < minimum_total:
        return {
            **base,
            "status": "insufficient_data",
            "reason": "not_enough_unique_resolved_markets_for_train_and_holdout_folds",
            "minimum_total_unique_markets": minimum_total,
        }

    first_test_index = max(minimum_train_markets, int(len(ordered) * train_fraction))
    holdout = ordered[first_test_index:]
    if len(holdout) < folds * minimum_test_markets:
        return {
            **base,
            "status": "insufficient_data",
            "reason": "holdout_period_too_small_for_requested_folds",
            "minimum_total_unique_markets": minimum_total,
        }

    fold_reports = []
    fold_passes = []
    for index in range(folds):
        start = len(holdout) * index // folds
        end = len(holdout) * (index + 1) // folds
        test_rows = holdout[start:end]
        if not test_rows:
            continue
        test_start = test_rows[0]["_decision_dt"]
        # Strict purge: even an earlier market is not training data until its
        # definitive label was available before this fold began.
        train_rows = [
            row for row in ordered[:first_test_index + start]
            if row["_decision_dt"] < test_start
            and row["_resolved_dt"] < test_start
        ]
        fold_report: dict[str, Any] = {
            "fold": index + 1,
            "test_decision_start": test_start.isoformat(),
            "test_decision_end": test_rows[-1]["_decision_dt"].isoformat(),
            "training_markets_resolved_before_test_start": len(train_rows),
            "test_markets": len(test_rows),
            "latest_training_resolution_at": max(
                (row["_resolved_dt"] for row in train_rows), default=None
            ).isoformat() if train_rows else None,
            "selected_threshold_multiplier": None,
            "training_candidate_scores": [],
            "status": "insufficient_data",
        }
        if len(train_rows) < minimum_train_markets or len(test_rows) < minimum_test_markets:
            fold_report["reason"] = "purged_training_or_test_sample_below_minimum"
            fold_passes.append(False)
            fold_reports.append(fold_report)
            continue

        candidate_scores = []
        for multiplier in (1.0, 1.1, 1.2, 1.35, 1.5):
            selected = _apply_multiplier(train_rows, multiplier)
            if len(selected) < minimum_train_markets:
                continue
            stats = _metrics(selected)
            candidate_scores.append({
                "threshold_multiplier": multiplier,
                "selected_train_markets": len(selected),
                "train_net_pnl_usd": stats["total_net_pnl_usd"],
                "train_net_roi_pct": stats["net_roi_pct"],
            })
        fold_report["training_candidate_scores"] = candidate_scores
        if not candidate_scores:
            fold_report["reason"] = "no_threshold_candidate_has_enough_purged_training_markets"
            fold_passes.append(False)
            fold_reports.append(fold_report)
            continue

        # The threshold is selected strictly on data whose resolution was
        # already known before the test period began.
        selected_candidate = max(
            candidate_scores,
            key=lambda row: (
                row["train_net_roi_pct"],
                row["train_net_pnl_usd"],
                -row["threshold_multiplier"],
            ),
        )
        multiplier = float(selected_candidate["threshold_multiplier"])
        baseline_test = list(test_rows)
        candidate_test = _apply_multiplier(test_rows, multiplier)
        baseline_stats = _metrics(baseline_test)
        candidate_stats = _metrics(candidate_test)
        roi_improvement = (
            candidate_stats["net_roi_pct"] - baseline_stats["net_roi_pct"]
            if candidate_stats["net_roi_pct"] is not None and baseline_stats["net_roi_pct"] is not None
            else None
        )
        passed = (
            len(candidate_test) >= minimum_test_markets
            and candidate_stats["total_net_pnl_usd"] > 0
            and candidate_stats["net_roi_pct"] is not None
            and candidate_stats["net_roi_pct"] > 0
            and candidate_stats["total_net_pnl_usd"] >= baseline_stats["total_net_pnl_usd"]
            and roi_improvement is not None
            and roi_improvement >= 0.1
        )
        fold_report.update({
            "selected_threshold_multiplier": multiplier,
            "candidate_test_markets": len(candidate_test),
            "baseline_test_metrics": baseline_stats,
            "candidate_test_metrics": candidate_stats,
            "net_roi_improvement_percentage_points": (
                round(roi_improvement, 8) if roi_improvement is not None else None
            ),
            "total_net_pnl_improvement_usd": round(
                candidate_stats["total_net_pnl_usd"] - baseline_stats["total_net_pnl_usd"], 8
            ),
            "status": "passed" if passed else "rejected",
            "reason": (
                "positive_net_roi_and_pnl_with_improvement_and_no_total_pnl_reduction"
                if passed else
                "candidate_failed_positive_net_roi_pnl_sample_or_baseline_comparison"
            ),
        })
        fold_passes.append(passed)
        fold_reports.append(fold_report)

    scored = sum(1 for row in fold_reports if row["status"] in {"passed", "rejected"})
    passed_count = sum(1 for row in fold_reports if row["status"] == "passed")
    all_passed = len(fold_passes) == folds and all(fold_passes)
    return {
        **base,
        "status": "scored" if scored == folds else "insufficient_fold_coverage",
        "reason": (
            "all_folds_passed_exploratory_consistency_gate"
            if all_passed else
            "one_or_more_folds_failed_or_lacked_sample_coverage"
        ),
        "folds": fold_reports,
        "folds_scored": scored,
        "folds_passed": passed_count,
        "walk_forward_consistency_gate_passed": all_passed,
        # A consistency gate is deliberately not equated with robust statistical
        # validation, production readiness, or live-money authorization.
        "out_of_sample_validated": False,
    }


def evaluate_resolved_event_entries(
    paper_database_path: str | Path,
    market_history_database_path: str | Path,
    resolution_log_path: str | Path,
    *,
    resolution_backup_count: int = 3,
    max_entry_snapshot_age_seconds: float = 300.0,
    settlement_fee_rate: float = 0.0,
    train_fraction: float = 0.5,
    folds: int = 3,
    minimum_train_markets: int = 20,
    minimum_test_markets: int = 10,
) -> dict[str, Any]:
    max_age = _number(
        max_entry_snapshot_age_seconds, "max_entry_snapshot_age_seconds",
        low=1.0, high=86_400.0,
    )
    settlement_fee = _number(
        settlement_fee_rate, "settlement_fee_rate", low=0.0, high=0.20
    )
    resolutions, conflicting, resolution_diagnostics = load_definitive_resolutions(
        resolution_log_path,
        backup_count=resolution_backup_count,
    )
    decision_pairs, decision_diagnostics = _load_decision_trade_pairs(paper_database_path)
    history = _readonly_db(market_history_database_path)
    try:
        _require_columns(
            history,
            "market_snapshots",
            {
                "snapshot_id", "market_id", "observed_at", "captured_at",
                "yes_ask", "no_ask", "yes_book_json", "no_book_json",
            },
        )
        evaluated: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        unresolved_count = 0
        for decision in decision_pairs:
            base_record = {
                "decision_id": decision.get("decision_id"),
                "market_id": str(decision.get("market_id") or ""),
                "direction": str(decision.get("selected_direction") or "").upper(),
            }
            if decision.get("_invalid_reason"):
                excluded.append({**base_record, "reason": decision["_invalid_reason"]})
                continue
            market_id = base_record["market_id"]
            resolution = resolutions.get(market_id)
            if resolution is None:
                if market_id in conflicting:
                    excluded.append({**base_record, "reason": conflicting[market_id]})
                else:
                    unresolved_count += 1
                continue

            snapshot, snapshot_error = _entry_snapshot(
                history, market_id, decision["_decision_dt"],
                max_age_seconds=max_age,
            )
            if snapshot_error:
                excluded.append({**base_record, "reason": snapshot_error})
                continue
            scored, reason = _validate_and_score_entry(
                decision, resolution, snapshot,
                settlement_fee_rate=settlement_fee,
            )
            if reason:
                excluded.append({**base_record, "reason": reason})
                continue
            evaluated.append(scored)
    finally:
        history.close()

    # One market has one eventual binary outcome. Keep only the earliest
    # eligible entry so repeated signals on the same condition cannot inflate
    # sample size or pretend the outcomes are independent.
    unique_samples: list[dict[str, Any]] = []
    seen_markets: set[str] = set()
    for row in sorted(evaluated, key=lambda item: (item["decision_at"], item["trade_id"])):
        market_id = row["market_id"]
        if market_id in seen_markets:
            excluded.append({
                "decision_id": row["decision_id"],
                "market_id": market_id,
                "direction": row["direction"],
                "reason": "duplicate_market_decision_after_first_eligible_entry",
            })
            continue
        seen_markets.add(market_id)
        unique_samples.append(row)

    # Attach private parsed timestamps only for fold arithmetic; remove them
    # from serialized trade records below.
    fold_samples = []
    for row in unique_samples:
        fold_samples.append({
            **row,
            "_decision_dt": _utc(row["decision_at"], "decision_at"),
            "_resolved_dt": _utc(row["market_resolved_at"], "market_resolved_at"),
        })
    walk_forward = chronological_resolution_walk_forward(
        fold_samples,
        train_fraction=train_fraction,
        folds=folds,
        minimum_train_markets=minimum_train_markets,
        minimum_test_markets=minimum_test_markets,
    )
    aggregate = _metrics(unique_samples)
    return {
        "mode": "RESOLUTION_ALIGNED_EVENT_ENTRY_EVALUATION_V1",
        "status": "scored" if unique_samples else "insufficient_data",
        "metrics": aggregate,
        "coverage": {
            **decision_diagnostics,
            **resolution_diagnostics,
            "definitive_resolutions_available": len(resolutions),
            "conflicting_resolution_markets_withheld": len(conflicting),
            "entered_decisions_without_resolution_yet": unresolved_count,
            "eligible_unique_resolved_markets": len(unique_samples),
            "excluded_decision_rows": len(excluded),
            "deduplication_policy": "earliest_eligible_entry_per_resolved_market",
        },
        "walk_forward": walk_forward,
        "parameters": {
            "max_entry_snapshot_age_seconds": max_age,
            "settlement_fee_rate_assumption": settlement_fee,
            "train_fraction": train_fraction,
            "folds": folds,
            "minimum_train_markets": minimum_train_markets,
            "minimum_test_markets": minimum_test_markets,
            "entry_price": "replayed_top5_ask_vwap_matched_to_paper_trade_ledger",
            "entry_time": "latest_archive_capture_and_observation_at_or_before_decision",
            "settlement": "binary_one_dollar_payout_per_winning_share",
            "settlement_fee": "explicit configurable assumption; defaults to zero and must be verified for the target account/market",
        },
        "trades": unique_samples,
        "excluded": excluded,
        "limitations": [
            "Settlement PnL is a counterfactual hold-to-resolution outcome, not the paper engine's actual stop-loss, take-profit, or time-based exit PnL.",
            "Only markets with definitive exact binary resolution records retained in the configured JSONL history are included; unresolved and disputed markets are withheld.",
            "The first eligible entry per market is kept to avoid counting repeated predictions on the same binary outcome as independent samples.",
            "The report validates the recorded ask-side top-five entry fill against the paper trade ledger; it does not recreate news signals from historical raw articles.",
            "The settlement payout assumes one dollar per winning share. Settlement/withdrawal costs are not verified by this research module and are controlled only by the explicit settlement_fee_rate assumption.",
            "Walk-forward threshold selection is exploratory, tests only stricter filters of entries already taken by the historical policy, and purges training outcomes not known before each fold's first decision.",
            "Related markets, shared news events, source dependence, market selection, resolution-log retention, and survivorship can still bias the sample; consistency is not proof of a durable trading edge.",
        ],
        "out_of_sample_validated": False,
        "walk_forward_consistency_gate_passed": walk_forward["walk_forward_consistency_gate_passed"],
        "live_execution_eligible": False,
        "real_money_execution": "not_implemented",
        "research_only": True,
        "execution": "none",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate event-paper entries against definitive binary market resolutions."
    )
    parser.add_argument("--paper-db", required=True)
    parser.add_argument("--market-history-db", required=True)
    parser.add_argument("--resolution-log", default="data/resolved_markets.jsonl")
    parser.add_argument("--resolution-backups", type=int, default=3)
    parser.add_argument("--max-entry-age-seconds", type=float, default=300.0)
    parser.add_argument("--settlement-fee-rate", type=float, default=0.0)
    parser.add_argument("--train-fraction", type=float, default=0.5)
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--minimum-train-markets", type=int, default=20)
    parser.add_argument("--minimum-test-markets", type=int, default=10)
    parser.add_argument("--output")
    args = parser.parse_args()

    report = evaluate_resolved_event_entries(
        args.paper_db,
        args.market_history_db,
        args.resolution_log,
        resolution_backup_count=args.resolution_backups,
        max_entry_snapshot_age_seconds=args.max_entry_age_seconds,
        settlement_fee_rate=args.settlement_fee_rate,
        train_fraction=args.train_fraction,
        folds=args.folds,
        minimum_train_markets=args.minimum_train_markets,
        minimum_test_markets=args.minimum_test_markets,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    print(rendered)
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
