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

from perps_market_data import PerpsPublicMarketDataProvider
from perps_market_history_store import PerpsMarketHistoryStore


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
) -> int:
    """Poll read-only Perps market data, archive it, and retry with bounded backoff."""
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
    state: dict[str, Any] = {
        "schema_version": 1, "enabled": True, "worker_state": "starting",
        "last_heartbeat_at": _utc_now(), "last_success_at": None,
        "last_error": None, "consecutive_failures": 0,
        "markets_in_last_snapshot": 0, "snapshots_inserted_total": 0,
        "history_rows": current_store.count(), "poll_interval_seconds": interval,
        "history_retention_days": retention_days, "expired_rows_pruned_total": 0,
        "research_only": True, "live_execution_enabled": False, "execution": "none",
    }
    cycles = 0
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
                state["worker_state"] = "healthy"
                state["last_success_at"] = _utc_now()
                state["last_error"] = None
                state["consecutive_failures"] = 0
                state["markets_in_last_snapshot"] = len(snapshots)
                state["snapshots_inserted_total"] += int(recorded["inserted"])
                state["history_rows"] = int(current_store.count())
                state["last_heartbeat_at"] = _utc_now()
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
