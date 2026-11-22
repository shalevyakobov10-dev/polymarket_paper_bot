"""Point-in-time historical replay for the isolated-margin Perps paper engine.

The replay uses only public snapshots and settlement rows whose original
received_at timestamp is no later than the simulated decision time. It never
submits orders and is intended for research, not live-trading eligibility.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from perps_market_history_store import PerpsMarketHistoryStore
from perps_paper_config import perps_engine_options_from_env
from perps_paper_engine import PerpsPaperEngine


def _parse_aware(value: str | datetime, field: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a valid ISO timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


class PointInTimePerpsHistory:
    """History adapter that exposes only data received by a simulated timestamp."""

    def __init__(self, store: PerpsMarketHistoryStore, as_of: str | datetime) -> None:
        self._store = store
        self._as_of = _parse_aware(as_of, "as_of")

    def history(self, instrument_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return self._store.history(
            instrument_id,
            limit=limit,
            as_of_received_at=self._as_of,
        )

    def funding_rate_at(
        self,
        instrument_id: int,
        timestamp_ms: int,
        *,
        tolerance_ms: int = 1000,
    ) -> dict[str, Any] | None:
        return self._store.funding_rate_at(
            instrument_id,
            timestamp_ms,
            tolerance_ms=tolerance_ms,
            as_of_received_at=self._as_of,
        )


def _engine_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    instrument_id = int(row["instrument_id"])
    return {
        "instrument": row["instrument"],
        "ticker": row["ticker"],
        "book": {
            "instrument_id": instrument_id,
            "timestamp_ms": int(row["book_timestamp_ms"]),
            "sequence": int(row["book_sequence"]),
            "best_bid": row["best_bid"],
            "best_ask": row["best_ask"],
            "bids": row["bids"],
            "asks": row["asks"],
        },
        "fees": row["fees"],
        "received_at": row["received_at"],
        "research_only": True,
        "execution": "none",
    }


def replay_perps_history(
    history_database_path: str | Path,
    *,
    start_received_at: str | datetime | None = None,
    end_received_at: str | datetime | None = None,
    max_snapshots: int = 10_000,
    engine_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay a bounded archived Perps window through a fresh paper account.

    Rows are consumed in capture order. Each strategy decision receives an
    as-of history adapter that filters both market snapshots and funding
    settlements by their original received_at timestamp before SQL LIMIT.
    """
    source_path = Path(history_database_path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError("Perps market-history database does not exist")
    if isinstance(max_snapshots, bool) or not isinstance(max_snapshots, int) or not 1 <= max_snapshots <= 100_000:
        raise ValueError("max_snapshots must be an integer between 1 and 100000")
    if engine_options is not None and not isinstance(engine_options, dict):
        raise ValueError("engine_options must be an object")

    store = PerpsMarketHistoryStore(source_path)
    try:
        rows = store.snapshots(
            start_received_at=start_received_at,
            end_received_at=end_received_at,
            limit=max_snapshots,
        )
        if not rows:
            raise ValueError("no archived Perps snapshots exist in the requested replay window")

        with tempfile.TemporaryDirectory(prefix="perps-paper-replay-") as scratch:
            engine_path = Path(scratch) / "isolated-paper-replay.sqlite3"
            # Use the same risk and signal settings as the supervised Paper
            # worker by default; explicit engine_options are research overrides.
            options = perps_engine_options_from_env()
            options.update(engine_options or {})
            with PerpsPaperEngine(engine_path, **options) as engine:
                instrument_ids: set[int] = set()
                for row in rows:
                    snapshot = _engine_snapshot(row)
                    received_at = _parse_aware(snapshot["received_at"], "received_at")
                    point_in_time_history = PointInTimePerpsHistory(store, received_at)
                    engine.process_snapshot(snapshot, point_in_time_history, now=received_at)
                    instrument_ids.add(int(row["instrument_id"]))

                summary = engine.summary()
                entry_count = int(engine.db.execute(
                    "SELECT COUNT(*) FROM perps_paper_events "
                    "WHERE event_type='PAPER_POSITION_OPENED'"
                ).fetchone()[0])
                first_entry = engine.db.execute(
                    "SELECT MIN(event_at) FROM perps_paper_events "
                    "WHERE event_type='PAPER_POSITION_OPENED'"
                ).fetchone()[0]
                first_received_at = rows[0]["received_at"]
                last_received_at = rows[-1]["received_at"]
                position_count = int(engine.db.execute(
                    "SELECT COUNT(*) FROM perps_paper_positions"
                ).fetchone()[0])
                all_trades = engine.db.execute(
                    """
                    SELECT position_id,instrument_id,symbol,side,quantity,entry_price,
                           exit_price,gross_pnl,net_pnl,opened_at,closed_at,close_reason,status
                    FROM perps_paper_positions
                    ORDER BY position_id
                    """
                ).fetchall()
                closed_trades = [row for row in all_trades if row["status"] in {"CLOSED", "LIQUIDATED"}]
                winning_pnl = sum(
                    (Decimal(row["net_pnl"]) for row in closed_trades if Decimal(row["net_pnl"]) > 0),
                    Decimal("0"),
                )
                losing_pnl = -sum(
                    (Decimal(row["net_pnl"]) for row in closed_trades if Decimal(row["net_pnl"]) < 0),
                    Decimal("0"),
                )
                net_closed = sum(
                    (Decimal(row["net_pnl"]) for row in closed_trades),
                    Decimal("0"),
                )
                gross_closed = sum(
                    (Decimal(row["gross_pnl"]) for row in closed_trades),
                    Decimal("0"),
                )
                win_count = sum(1 for row in closed_trades if Decimal(row["net_pnl"]) > 0)
                loss_count = sum(1 for row in closed_trades if Decimal(row["net_pnl"]) < 0)
                trade_log = [
                    {
                        key: row[key]
                        for key in (
                            "position_id", "instrument_id", "symbol", "side", "quantity",
                            "entry_price", "exit_price", "gross_pnl", "net_pnl",
                            "opened_at", "closed_at", "close_reason", "status",
                        )
                    }
                    for row in all_trades[-500:]
                ]

        return {
            "mode": "POLYMARKET_PERPS_POINT_IN_TIME_HISTORICAL_PAPER_REPLAY",
            "snapshots_replayed": len(rows),
            "instruments_replayed": len(instrument_ids),
            "instrument_ids": sorted(instrument_ids),
            "first_received_at": first_received_at,
            "last_received_at": last_received_at,
            "paper_entry_events": entry_count,
            "first_entry_at": first_entry,
            "position_records": position_count,
            "performance": {
                "closed_trades": len(closed_trades),
                "winning_trades": win_count,
                "losing_trades": loss_count,
                "win_rate_pct": (
                    str((Decimal(win_count) / Decimal(len(closed_trades)) * Decimal("100")).quantize(Decimal("0.01")))
                    if closed_trades else None
                ),
                "gross_closed_pnl": str(gross_closed),
                "net_closed_pnl_after_fees_and_funding": str(net_closed),
                "profit_factor": (
                    str((winning_pnl / losing_pnl).quantize(Decimal("0.0001")))
                    if losing_pnl > 0 else None
                ),
                "profit_factor_status": "calculated" if losing_pnl > 0 else "undefined_without_closed_losses",
            },
            "trade_log": trade_log,
            "trade_log_limit": 500,
            "trade_log_truncated": position_count > 500,
            "portfolio": summary,
            "lookahead_protection": {
                "market_history_filtered_by_received_at": True,
                "funding_history_filtered_by_received_at": True,
                "query_filters_applied_before_limit": True,
            },
            "funding_policy": (
                "Only settlements captured by the simulated decision time are visible; "
                "missing or late settlement data causes the paper engine to halt new risk."
            ),
            "research_only": True,
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
        description="Run a point-in-time, paper-only replay of archived Polymarket Perps data."
    )
    parser.add_argument(
        "--database",
        default=os.getenv("PERPS_MARKET_HISTORY_DB", "data/perps_market_history.sqlite3"),
        help="Path to the archived public Perps market-history SQLite database.",
    )
    parser.add_argument("--from", dest="start_received_at", help="Inclusive ISO-8601 capture-time start.")
    parser.add_argument("--to", dest="end_received_at", help="Inclusive ISO-8601 capture-time end.")
    parser.add_argument("--max-snapshots", type=int, default=10_000, help="Maximum replay rows (1–100000).")
    args = parser.parse_args(argv)
    try:
        report = replay_perps_history(
            args.database,
            start_received_at=args.start_received_at,
            end_received_at=args.end_received_at,
            max_snapshots=args.max_snapshots,
        )
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, f"Perps historical replay failed safely: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
