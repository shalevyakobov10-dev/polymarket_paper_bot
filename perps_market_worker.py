"""Resilient public Perps market-history collector; no account or execution access."""
from __future__ import annotations

import argparse
import json
import os
import signal
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backup_paper_data import create_paper_data_backups
from perps_market_data import PerpsPublicMarketDataProvider
from perps_market_history_store import PerpsMarketHistoryStore
from perps_paper_engine import PerpsPaperEngine


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, sort_keys=True, allow_nan=False)
    temp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent),
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp = handle.name
            handle.write(serialized + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        temp = None
    finally:
        if temp:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass


def _status_count(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def read_perps_worker_status(path: str | Path) -> dict[str, Any]:
    """Return a fixed allowlisted status view; discard unknown fields."""
    target = Path(path).expanduser()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if (
            not isinstance(raw, dict)
            or isinstance(raw.get("schema_version"), bool)
            or raw.get("schema_version") != 1
        ):
            raise ValueError("invalid Perps worker status schema")
        status = {
            "schema_version": 1,
            "enabled": raw.get("enabled") is True,
            "worker_state": raw.get("worker_state") if raw.get("worker_state") in {
                "starting", "healthy", "degraded", "stopped"
            } else "unknown",
            "last_heartbeat_at": raw.get("last_heartbeat_at"),
            "last_success_at": raw.get("last_success_at"),
            "last_error": str(raw.get("last_error"))[:240] if raw.get("last_error") else None,
            "consecutive_failures": _status_count(raw.get("consecutive_failures", 0), "consecutive_failures"),
            "markets_in_last_snapshot": _status_count(raw.get("markets_in_last_snapshot", 0), "markets_in_last_snapshot"),
            "snapshots_inserted_total": _status_count(raw.get("snapshots_inserted_total", 0), "snapshots_inserted_total"),
            "history_rows": _status_count(raw.get("history_rows", 0), "history_rows"),
            "poll_interval_seconds": _status_count(raw.get("poll_interval_seconds", 30), "poll_interval_seconds", minimum=1),
            "history_retention_days": _status_count(raw.get("history_retention_days", 14), "history_retention_days", minimum=1),
            "expired_rows_pruned_total": _status_count(raw.get("expired_rows_pruned_total", 0), "expired_rows_pruned_total"),
            "paper_engine_enabled": raw.get("paper_engine_enabled") is True,
            "paper_worker_state": raw.get("paper_worker_state") if raw.get("paper_worker_state") in {"disabled", "starting", "running", "degraded"} else "unknown",
            "paper_open_positions": _status_count(raw.get("paper_open_positions", 0), "paper_open_positions"),
            "paper_closed_trades": _status_count(raw.get("paper_closed_trades", 0), "paper_closed_trades"),
            "paper_closed_last_cycle": _status_count(raw.get("paper_closed_last_cycle", 0), "paper_closed_last_cycle"),
            "paper_funding_gaps_last_cycle": _status_count(raw.get("paper_funding_gaps_last_cycle", 0), "paper_funding_gaps_last_cycle"),
            "paper_snapshot_rejections_last_cycle": _status_count(raw.get("paper_snapshot_rejections_last_cycle", 0), "paper_snapshot_rejections_last_cycle"),
            "paper_cash_balance": str(raw.get("paper_cash_balance"))[:80] if raw.get("paper_cash_balance") is not None else None,
            "paper_equity": str(raw.get("paper_equity"))[:80] if raw.get("paper_equity") is not None else None,
            "paper_daily_loss": str(raw.get("paper_daily_loss"))[:80] if raw.get("paper_daily_loss") is not None else None,
            "backup_at": str(raw.get("backup_at"))[:80] if raw.get("backup_at") else None,
            "backup_error_type": str(raw.get("backup_error_type"))[:80] if raw.get("backup_error_type") else None,
            "research_only": True,
            "live_execution_enabled": False,
            "execution": "none",
        }
        return status
    except FileNotFoundError:
        return {
            "schema_version": 1, "enabled": False, "worker_state": "unknown",
            "last_heartbeat_at": None, "last_success_at": None,
            "last_error": "status_file_missing", "consecutive_failures": 0,
            "markets_in_last_snapshot": 0, "snapshots_inserted_total": 0,
            "history_rows": 0, "poll_interval_seconds": None,
            "history_retention_days": None, "expired_rows_pruned_total": 0,
            "research_only": True, "live_execution_enabled": False, "execution": "none",
        }
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return {
            "schema_version": 1, "enabled": False, "worker_state": "unknown",
            "last_heartbeat_at": None, "last_success_at": None,
            "last_error": "status_file_invalid", "consecutive_failures": 0,
            "markets_in_last_snapshot": 0, "snapshots_inserted_total": 0,
            "history_rows": 0, "poll_interval_seconds": None,
            "history_retention_days": None, "expired_rows_pruned_total": 0,
            "research_only": True, "live_execution_enabled": False, "execution": "none",
        }


def run_worker(
    *,
    provider: Any | None = None,
    store: Any | None = None,
    stop_event: Any | None = None,
    status_path: str | Path | None = None,
    poll_interval_seconds: int | None = None,
    history_retention_days: int | None = None,
    max_cycles: int | None = None,
    paper_engine: Any | None = None,
    paper_engine_factory: Any | None = None,
    paper_engine_enabled: bool | None = None,
    backup_function: Any = create_paper_data_backups,
    backup_interval_hours: int | None = None,
    backup_directory: str | Path | None = None,
    backup_retain: int | None = None,
) -> int:
    """Collect public Perps data, maintain a separate Paper account, and back up state."""
    stopping = stop_event or threading.Event()
    interval = poll_interval_seconds or _positive_int_env(
        "PERPS_POLL_INTERVAL_SECONDS", 30, 15, 300
    )
    if not 15 <= interval <= 300:
        raise ValueError("poll_interval_seconds must be between 15 and 300")
    retention_days = history_retention_days or _positive_int_env(
        "PERPS_HISTORY_RETENTION_DAYS", 14, 1, 365
    )
    if not 1 <= retention_days <= 365:
        raise ValueError("history_retention_days must be between 1 and 365")
    current_provider = provider or PerpsPublicMarketDataProvider(
        market_limit=_positive_int_env("PERPS_MARKET_LIMIT", 10, 1, 50),
        max_age_seconds=10.0,
        depth=10,
    )
    database = Path(os.getenv("PERPS_MARKET_HISTORY_DB", "data/perps_market_history.sqlite3"))
    current_store = store or PerpsMarketHistoryStore(database)
    status_file = Path(status_path or os.getenv(
        "PERPS_MARKET_WORKER_STATUS_FILE", "data/perps_market_worker_status.json"
    ))
    enabled_raw = str(os.getenv("ENABLE_PERPS_PAPER_ENGINE", "false")).strip().lower()
    if enabled_raw not in {"true", "false"}:
        raise ValueError("ENABLE_PERPS_PAPER_ENGINE must be exactly true or false")
    if paper_engine_enabled is None:
        paper_enabled = enabled_raw == "true" or paper_engine is not None
    else:
        if not isinstance(paper_engine_enabled, bool):
            raise ValueError("paper_engine_enabled must be boolean")
        paper_enabled = paper_engine_enabled
    if paper_engine is not None:
        paper_enabled = True
    current_paper_engine = paper_engine
    if paper_enabled and current_paper_engine is None:
        factory = paper_engine_factory or PerpsPaperEngine
        paper_database = Path(os.getenv(
            "PERPS_PAPER_DATABASE_PATH", "data/perps_paper.sqlite3"
        ))
        current_paper_engine = factory(
            paper_database,
            starting_cash=os.getenv("PERPS_PAPER_STARTING_CASH", "100"),
            taker_fee_rate=os.getenv("PERPS_PAPER_TAKER_FEE_RATE", "0.0005"),
            max_entry_impact_bps=os.getenv("PERPS_MAX_ENTRY_IMPACT_BPS", "100"),
            max_market_data_age_seconds="10",
            snapshot_retention_days=retention_days,
        )
    interval_backup = backup_interval_hours or _positive_int_env(
        "PERPS_BACKUP_INTERVAL_HOURS", 6, 1, 168
    )
    if not 1 <= interval_backup <= 168:
        raise ValueError("PERPS_BACKUP_INTERVAL_HOURS must be between 1 and 168")
    retain_backup = backup_retain or _positive_int_env(
        "PERPS_BACKUP_RETAIN", 30, 1, 365
    )
    if not 1 <= retain_backup <= 365:
        raise ValueError("PERPS_BACKUP_RETAIN must be between 1 and 365")
    backup_path = Path(backup_directory or os.getenv(
        "PERPS_BACKUP_DIRECTORY", str(Path(os.getenv("DATA_DIR", "data")) / "backups")
    ))
    state: dict[str, Any] = {
        "schema_version": 1, "enabled": True, "worker_state": "starting",
        "last_heartbeat_at": _utc_now(), "last_success_at": None,
        "last_error": None, "consecutive_failures": 0,
        "markets_in_last_snapshot": 0, "snapshots_inserted_total": 0,
        "history_rows": current_store.count(), "poll_interval_seconds": interval,
        "history_retention_days": retention_days, "expired_rows_pruned_total": 0,
        "paper_engine_enabled": paper_enabled,
        "paper_worker_state": "starting" if paper_enabled else "disabled",
        "paper_open_positions": 0,
        "paper_closed_trades": 0,
        "paper_closed_last_cycle": 0,
        "paper_funding_gaps_last_cycle": 0,
        "paper_snapshot_rejections_last_cycle": 0,
        "paper_cash_balance": None,
        "paper_equity": None,
        "paper_daily_loss": None,
        "backup_at": None,
        "backup_error_type": None,
        "research_only": True, "live_execution_enabled": False, "execution": "none",
    }
    cycles = 0
    last_backup_monotonic: float | None = None
    last_prune_monotonic: float | None = None
    try:
        _write_status(status_file, state)
        while not stopping.is_set():
            state["last_heartbeat_at"] = _utc_now()
            try:
                snapshots = current_provider.load()
                if not snapshots:
                    raise RuntimeError("provider returned zero usable Perps snapshots")
                recorded = current_store.record_snapshots(snapshots)
                if int(recorded["inserted"]) < 0:
                    raise RuntimeError("history store returned an invalid insert count")

                paper_closed = 0
                paper_funding_gaps = 0
                paper_rejections = 0
                paper_metrics = None
                if current_paper_engine is not None:
                    for snapshot in snapshots:
                        outcome = current_paper_engine.process_snapshot(snapshot)
                        if outcome.get("status") == "PAPER_POSITION_CLOSED":
                            paper_closed += 1
                        elif outcome.get("status") == "PAPER_POSITION_FUNDING_GAP":
                            paper_funding_gaps += 1
                        elif outcome.get("status") == "PAPER_SNAPSHOT_REJECTED":
                            paper_rejections += 1
                    paper_metrics = current_paper_engine.account_metrics()
                    if paper_metrics.get("stale_open_position"):
                        paper_rejections += 1

                monotonic_now = time.monotonic()
                prune_before = getattr(current_store, "prune_before", None)
                if callable(prune_before) and (
                    last_prune_monotonic is None
                    or monotonic_now - last_prune_monotonic >= 21_600
                ):
                    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
                    pruned = prune_before(cutoff)
                    deleted = int(pruned.get("deleted", 0))
                    if deleted < 0:
                        raise RuntimeError("history retention returned an invalid delete count")
                    state["expired_rows_pruned_total"] += deleted
                    state["history_rows"] = int(pruned.get("remaining", current_store.count()))
                    last_prune_monotonic = monotonic_now
                paper_warning = bool(paper_funding_gaps or paper_rejections)
                if current_paper_engine is not None and paper_metrics is not None:
                    paper_status = current_paper_engine.status()
                    state.update({
                        "paper_worker_state": "degraded" if paper_warning else "running",
                        "paper_open_positions": int(paper_metrics.get("open_positions") or 0),
                        "paper_closed_trades": int(paper_status.get("closed_trades") or 0),
                        "paper_closed_last_cycle": paper_closed,
                        "paper_funding_gaps_last_cycle": paper_funding_gaps,
                        "paper_snapshot_rejections_last_cycle": paper_rejections,
                        "paper_cash_balance": str(paper_metrics.get("cash_balance")),
                        "paper_equity": str(paper_metrics.get("equity")),
                        "paper_daily_loss": str(paper_metrics.get("daily_loss")),
                    })
                state["worker_state"] = "degraded" if paper_warning or state.get("backup_error_type") else "healthy"
                state["last_success_at"] = _utc_now()
                state["last_error"] = (
                    "paper_funding_gap_or_snapshot_rejection" if paper_warning else None
                )
                state["consecutive_failures"] = 0
                state["markets_in_last_snapshot"] = len(snapshots)
                state["snapshots_inserted_total"] += int(recorded["inserted"])
                state["history_rows"] = int(current_store.count())
                state["last_heartbeat_at"] = _utc_now()

                if backup_function is not None and (
                    last_backup_monotonic is None
                    or monotonic_now - last_backup_monotonic >= interval_backup * 3600
                ):
                    backup_sources = []
                    store_path = getattr(current_store, "path", None)
                    if store_path is not None and Path(store_path).exists():
                        backup_sources.append(Path(store_path))
                    paper_database = getattr(current_paper_engine, "database_path", None)
                    if paper_database is not None and Path(paper_database).exists():
                        backup_sources.append(Path(paper_database))
                    if backup_sources:
                        try:
                            backup_result = backup_function(
                                backup_sources, backup_path, retain=retain_backup
                            )
                            backed_up = backup_result.get("files") or []
                            if len(backed_up) != len(backup_sources):
                                raise RuntimeError("perps_backup_incomplete")
                            state["backup_at"] = str(backup_result.get("created_at") or _utc_now())
                            state["backup_error_type"] = None
                            last_backup_monotonic = monotonic_now
                        except Exception as exc:
                            state["backup_error_type"] = type(exc).__name__[:80]
                            state["worker_state"] = "degraded"
                _write_status(status_file, state)
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    return 0
                if stopping.wait(interval):
                    break
            except Exception as exc:
                failures = int(state["consecutive_failures"]) + 1
                delay = min(300.0, 2.0 * (2 ** min(failures - 1, 7)))
                state["worker_state"] = "degraded"
                state["last_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
                state["consecutive_failures"] = failures
                state["last_heartbeat_at"] = _utc_now()
                _write_status(status_file, state)
                if max_cycles is not None:
                    return 1
                if stopping.wait(delay):
                    break
        state["worker_state"] = "stopped"
        state["last_heartbeat_at"] = _utc_now()
        _write_status(status_file, state)
        return 0
    finally:
        if store is None:
            current_store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect public Polymarket Perps market data")
    parser.add_argument("--once", action="store_true", help="collect one snapshot cycle and exit")
    args = parser.parse_args()
    stop_event = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return run_worker(stop_event=stop_event, max_cycles=1 if args.once else None)


if __name__ == "__main__":
    raise SystemExit(main())
