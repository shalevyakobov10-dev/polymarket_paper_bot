"""Resilient public Perps market-history collector; no account or execution access."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backup_paper_data import create_paper_data_backups
from perps_market_data import PerpsPublicMarketDataProvider
from perps_market_history_store import PerpsMarketHistoryStore
from perps_paper_config import perps_engine_options_from_env
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


def _sanitize_paper_summary(value: Any) -> dict[str, Any] | None:
    """Allowlist paper portfolio telemetry and never expose raw database rows."""
    if not isinstance(value, dict):
        return None
    summary: dict[str, Any] = {
        "mode": "POLYMARKET_PERPS_ISOLATED_MARGIN_PAPER",
        "research_only": True,
        "live_execution_enabled": False,
        "order_submission_allowed": False,
        "execution": "none",
        "margin_mode": "isolated",
        "cross_margin_supported": False,
        "maintenance_margin_rule": "0.5 / instrument_max_leverage",
        "liquidation_is_venue_exact": False,
        "liquidation_model_limitations": "no insurance-fund backstop or auto-deleveraging simulation",
    }
    amount_fields = (
        "cash", "equity", "peak_equity", "isolated_margin_reserved",
        "unrealized_pnl", "closed_net_pnl", "daily_start_equity",
        "max_leverage", "max_position_notional", "max_total_notional",
        "max_entry_impact_bps", "taker_fee_rate_assumption", "liquidation_fee_rate_assumption",
    )
    for field in amount_fields:
        raw = value.get(field)
        if raw is None:
            continue
        text_value = str(raw).strip()
        if len(text_value) > 80:
            continue
        try:
            from decimal import Decimal, InvalidOperation
            parsed = Decimal(text_value)
            if parsed.is_finite():
                summary[field] = text_value
        except (InvalidOperation, ValueError, TypeError):
            continue
    for field in ("open_positions", "closed_positions"):
        raw = value.get(field)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            summary[field] = raw
    if value.get("halted") in (True, False):
        summary["halted"] = value["halted"]
    if value.get("halt_reason") is None or isinstance(value.get("halt_reason"), str):
        summary["halt_reason"] = str(value.get("halt_reason"))[:120] if value.get("halt_reason") else None
    if isinstance(value.get("utc_day"), str):
        summary["utc_day"] = value["utc_day"][:10]
    performance = value.get("performance")
    if isinstance(performance, dict):
        safe_performance: dict[str, Any] = {
            "descriptive_only": True,
            "out_of_sample_validated": False,
            "live_execution_eligible": False,
        }
        for field in (
            "closed_trades", "winning_trades", "losing_trades", "breakeven_trades",
        ):
            raw = performance.get(field)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                safe_performance[field] = raw
        decimal_fields = (
            "win_rate_pct", "gross_closed_pnl", "net_closed_pnl_after_fees_and_funding",
            "net_expectancy_per_closed_trade", "profit_factor", "entry_fees_paid",
            "exit_fees_paid", "liquidation_fees_paid", "total_fees_paid", "funding_paid",
        )
        for field in decimal_fields:
            raw = performance.get(field)
            if raw is None:
                safe_performance[field] = None
                continue
            text_value = str(raw).strip()
            if len(text_value) > 80:
                continue
            try:
                from decimal import Decimal, InvalidOperation
                parsed = Decimal(text_value)
                if parsed.is_finite():
                    safe_performance[field] = text_value
            except (InvalidOperation, ValueError, TypeError):
                continue
        if performance.get("profit_factor_status") in {
            "calculated", "undefined_without_closed_losses", "no_closed_trades",
        }:
            safe_performance["profit_factor_status"] = performance["profit_factor_status"]
        summary["performance"] = safe_performance
    if isinstance(value.get("event_counts"), dict):
        counts = {}
        for key, count in list(value["event_counts"].items())[:30]:
            if isinstance(key, str) and isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                counts[key[:80]] = count
        summary["event_counts"] = counts
    return summary


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
        missing_open_ids = raw.get("open_position_instruments_missing", [])
        if (
            not isinstance(missing_open_ids, list)
            or len(missing_open_ids) > 10
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in missing_open_ids
            )
        ):
            raise ValueError("invalid missing open-position instrument IDs")
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
            "markets_with_fee_schedule": _status_count(raw.get("markets_with_fee_schedule", 0), "markets_with_fee_schedule"),
            "snapshots_inserted_total": _status_count(raw.get("snapshots_inserted_total", 0), "snapshots_inserted_total"),
            "history_rows": _status_count(raw.get("history_rows", 0), "history_rows"),
            "poll_interval_seconds": _status_count(raw.get("poll_interval_seconds", 30), "poll_interval_seconds", minimum=1),
            "heartbeat_timeout_seconds": _status_count(
                raw.get("heartbeat_timeout_seconds", 180),
                "heartbeat_timeout_seconds",
                minimum=60,
            ),
            "history_retention_days": _status_count(raw.get("history_retention_days", 14), "history_retention_days", minimum=1),
            "expired_rows_pruned_total": _status_count(raw.get("expired_rows_pruned_total", 0), "expired_rows_pruned_total"),
            "paper_engine_enabled": raw.get("paper_engine_enabled") is True,
            "paper_engine_state": raw.get("paper_engine_state") if raw.get("paper_engine_state") in {
                "disabled", "starting", "healthy", "halted", "degraded"
            } else "unknown",
            "paper_engine_degraded_reason": (
                str(raw.get("paper_engine_degraded_reason"))[:120]
                if isinstance(raw.get("paper_engine_degraded_reason"), str)
                else None
            ),
            "open_position_instruments_missing": sorted(set(missing_open_ids)),
            "paper_engine": _sanitize_paper_summary(raw.get("paper_engine")),
            "last_backup_at": raw.get("last_backup_at"),
            "last_backup_manifest_name": str(raw.get("last_backup_manifest_name") or "")[:200] or None,
            "backup_files_last_run": _status_count(raw.get("backup_files_last_run", 0), "backup_files_last_run"),
            "backup_error_type": str(raw.get("backup_error_type") or "")[:80] or None,
            "backup_consecutive_failures": _status_count(raw.get("backup_consecutive_failures", 0), "backup_consecutive_failures"),
            "backup_retry_delay_seconds": _status_count(raw.get("backup_retry_delay_seconds", 0), "backup_retry_delay_seconds"),
            "backup_interval_hours": _status_count(raw.get("backup_interval_hours", 6), "backup_interval_hours", minimum=1),
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
            "markets_in_last_snapshot": 0, "markets_with_fee_schedule": 0,
            "snapshots_inserted_total": 0,
            "history_rows": 0, "poll_interval_seconds": None,
            "heartbeat_timeout_seconds": 180,
            "history_retention_days": None, "expired_rows_pruned_total": 0,
            "paper_engine_enabled": False, "paper_engine_state": "unknown",
            "paper_engine_degraded_reason": None, "open_position_instruments_missing": [],
            "paper_engine": None,
            "research_only": True, "live_execution_enabled": False, "execution": "none",
        }
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return {
            "schema_version": 1, "enabled": False, "worker_state": "unknown",
            "last_heartbeat_at": None, "last_success_at": None,
            "last_error": "status_file_invalid", "consecutive_failures": 0,
            "markets_in_last_snapshot": 0, "markets_with_fee_schedule": 0,
            "snapshots_inserted_total": 0,
            "history_rows": 0, "poll_interval_seconds": None,
            "heartbeat_timeout_seconds": 180,
            "history_retention_days": None, "expired_rows_pruned_total": 0,
            "paper_engine_enabled": False, "paper_engine_state": "unknown",
            "paper_engine_degraded_reason": None, "open_position_instruments_missing": [],
            "paper_engine": None,
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
    paper_engine: Any | None = None,
    paper_engine_enabled: bool | None = None,
    backup_function: Any | None = create_paper_data_backups,
    backup_interval_hours: int | None = None,
    backup_directory: str | Path | None = None,
    backup_retain: int | None = None,
    max_consecutive_failures: int | None = None,
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
    failure_limit = (
        _positive_int_env("PERPS_WORKER_MAX_CONSECUTIVE_FAILURES", 10, 1, 50)
        if max_consecutive_failures is None else max_consecutive_failures
    )
    if isinstance(failure_limit, bool) or not isinstance(failure_limit, int) or not 1 <= failure_limit <= 50:
        raise ValueError("max_consecutive_failures must be an integer between 1 and 50")
    market_limit = _positive_int_env("PERPS_MARKET_LIMIT", 10, 1, 50)
    minimum_heartbeat_timeout = max(180, interval * 3 + market_limit * 8 + 80)
    heartbeat_timeout = _positive_int_env(
        "PERPS_WORKER_HEARTBEAT_TIMEOUT_SECONDS",
        minimum_heartbeat_timeout,
        minimum_heartbeat_timeout,
        7200,
    )
    current_provider = provider or PerpsPublicMarketDataProvider(
        market_limit=market_limit,
        max_age_seconds=10.0,
        depth=10,
    )
    database = Path(os.getenv("PERPS_MARKET_HISTORY_DB", "data/perps_market_history.sqlite3")).expanduser()
    paper_database = Path(os.getenv(
        "PERPS_PAPER_DB",
        str(Path(os.getenv("DATA_DIR", "data")) / "perps_paper.sqlite3"),
    )).expanduser()
    backup_interval = (
        _positive_int_env("PERPS_BACKUP_INTERVAL_HOURS", 6, 1, 168)
        if backup_interval_hours is None else backup_interval_hours
    )
    if isinstance(backup_interval, bool) or not isinstance(backup_interval, int) or not 1 <= backup_interval <= 168:
        raise ValueError("backup_interval_hours must be an integer between 1 and 168")
    retain_backups = (
        _positive_int_env("PERPS_BACKUP_RETAIN", 30, 1, 365)
        if backup_retain is None else backup_retain
    )
    if isinstance(retain_backups, bool) or not isinstance(retain_backups, int) or not 1 <= retain_backups <= 365:
        raise ValueError("backup_retain must be an integer between 1 and 365")
    chosen_backup_directory = Path(backup_directory or os.getenv(
        "PERPS_BACKUP_DIRECTORY",
        str(Path(os.getenv("DATA_DIR", "data")) / "perps_backups"),
    )).expanduser()
    current_store = store or PerpsMarketHistoryStore(database)
    status_file = Path(status_path or os.getenv(
        "PERPS_MARKET_WORKER_STATUS_FILE", "data/perps_market_worker_status.json"
    ))
    if paper_engine_enabled is None:
        paper_raw = str(os.getenv("ENABLE_PERPS_PAPER_ENGINE", "false")).strip().lower()
        if paper_raw not in {"true", "false"}:
            raise ValueError("ENABLE_PERPS_PAPER_ENGINE must be exactly true or false")
        engine_enabled = paper_raw == "true"
    else:
        if not isinstance(paper_engine_enabled, bool):
            raise ValueError("paper_engine_enabled must be a boolean")
        engine_enabled = paper_engine_enabled
    current_engine = paper_engine
    engine_created = False
    if engine_enabled and current_engine is None:
        current_engine = PerpsPaperEngine(
            os.getenv("PERPS_PAPER_DB", str(Path(os.getenv("DATA_DIR", "data")) / "perps_paper.sqlite3")),
            **perps_engine_options_from_env(),
        )
        engine_created = True
    if current_engine is not None and not engine_enabled:
        raise ValueError("paper_engine was supplied while paper_engine_enabled is false")
    initial_paper_summary = current_engine.summary() if current_engine is not None else None
    state: dict[str, Any] = {
        "schema_version": 1, "enabled": True, "worker_state": "starting",
        "last_heartbeat_at": _utc_now(), "last_success_at": None,
        "last_error": None, "consecutive_failures": 0,
        "markets_in_last_snapshot": 0, "snapshots_inserted_total": 0,
        "history_rows": current_store.count(), "poll_interval_seconds": interval,
        "heartbeat_timeout_seconds": heartbeat_timeout,
        "history_retention_days": retention_days, "expired_rows_pruned_total": 0,
        "paper_engine_enabled": engine_enabled,
        "paper_engine_state": (
            "halted" if initial_paper_summary and initial_paper_summary.get("halted")
            else "starting" if current_engine is not None else "disabled"
        ),
        "paper_engine_degraded_reason": None,
        "markets_with_fee_schedule": 0,
        "paper_engine": initial_paper_summary,
        "last_backup_at": None,
        "last_backup_manifest_name": None,
        "backup_files_last_run": 0,
        "backup_error_type": None,
        "backup_consecutive_failures": 0,
        "backup_retry_delay_seconds": 0,
        "backup_interval_hours": backup_interval,
        "research_only": True, "live_execution_enabled": False, "execution": "none",
    }
    cycles = 0
    last_prune_monotonic: float | None = None
    last_backup_attempt_at: datetime | None = None
    last_backup_monotonic: float | None = None
    try:
        _write_status(status_file, state)
        while not stopping.is_set():
            state["last_heartbeat_at"] = _utc_now()
            try:
                open_position_ids: list[int] = []
                if current_engine is not None:
                    get_open_ids = getattr(current_engine, "open_instrument_ids", None)
                    if callable(get_open_ids):
                        raw_open_ids = get_open_ids()
                        if not isinstance(raw_open_ids, (list, tuple, set)):
                            raise RuntimeError("paper engine returned an invalid open-position ID list")
                        if len(raw_open_ids) > 10 or any(
                            isinstance(value, bool) or not isinstance(value, int) or value < 0
                            for value in raw_open_ids
                        ):
                            raise RuntimeError("paper engine returned invalid open-position instrument IDs")
                        open_position_ids = sorted(set(raw_open_ids))
                if open_position_ids:
                    snapshots = current_provider.load(
                        priority_instrument_ids=open_position_ids
                    )
                else:
                    snapshots = current_provider.load()
                if not snapshots:
                    raise RuntimeError("provider returned zero usable Perps snapshots")
                snapshot_instrument_ids = {
                    row.get("instrument", {}).get("instrument_id")
                    for row in snapshots
                    if isinstance(row, dict) and isinstance(row.get("instrument"), dict)
                }
                missing_open_ids = sorted(
                    set(open_position_ids) - snapshot_instrument_ids
                )
                state["open_position_instruments_missing"] = missing_open_ids
                recorded = current_store.record_snapshots(snapshots)
                if int(recorded["inserted"]) < 0:
                    raise RuntimeError("history store returned an invalid insert count")
                if current_engine is not None:
                    funding_requirements = current_engine.funding_requirements(snapshots)
                    unique_funding_requirements = {}
                    for requirement in funding_requirements:
                        key = (
                            requirement.get("instrument_id"),
                            requirement.get("start_timestamp_ms"),
                            requirement.get("end_timestamp_ms"),
                        )
                        unique_funding_requirements[key] = requirement
                    for requirement in unique_funding_requirements.values():
                        try:
                            settlements = current_provider.load_funding_history(
                                requirement["instrument_id"],
                                start_timestamp_ms=requirement["start_timestamp_ms"],
                                end_timestamp_ms=requirement["end_timestamp_ms"],
                            )
                            current_store.record_funding_history(
                                requirement["instrument_id"],
                                settlements,
                                received_at=datetime.now(timezone.utc),
                            )
                        except Exception:
                            # The engine detects missing/ambiguous published
                            # settlements and halts new entries; exits still run.
                            continue
                    for market_snapshot in snapshots:
                        current_engine.process_snapshot(
                            market_snapshot,
                            current_store,
                            allow_new_entries=not bool(missing_open_ids),
                        )
                    paper_summary = current_engine.summary()
                    state["paper_engine"] = paper_summary
                    state["paper_engine_state"] = (
                        "halted" if paper_summary.get("halted") else "starting"
                    )
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
                fee_market_count = sum(
                    1 for row in snapshots
                    if isinstance(row.get("fees"), dict)
                    and row["fees"].get("source") == "public_perps_fee_schedule"
                )
                state["markets_with_fee_schedule"] = fee_market_count
                if current_engine is not None:
                    paper_summary = state.get("paper_engine") or {}
                    if paper_summary.get("halted"):
                        state["paper_engine_state"] = "halted"
                        state["paper_engine_degraded_reason"] = paper_summary.get("halt_reason")
                    elif missing_open_ids:
                        # Continue managing any available positions, but do not
                        # create fresh exposure while even one open instrument
                        # is missing a fresh, usable snapshot.
                        state["paper_engine_state"] = "degraded"
                        state["paper_engine_degraded_reason"] = "open_position_market_data_missing"
                    elif fee_market_count == 0:
                        # Market data ingestion remains healthy, but the paper
                        # engine cannot open new risk without verified fees.
                        state["paper_engine_state"] = "degraded"
                        state["paper_engine_degraded_reason"] = "no_verified_fee_schedule"
                    else:
                        state["paper_engine_state"] = "healthy"
                        state["paper_engine_degraded_reason"] = None
                state["snapshots_inserted_total"] += int(recorded["inserted"])
                state["history_rows"] = int(current_store.count())
                state["last_heartbeat_at"] = _utc_now()

                # Back up the position ledger and its matching market/funding
                # history together after a successful paper-engine cycle.
                if (
                    current_engine is not None
                    and backup_function is not None
                    and paper_database.is_file()
                    and database.is_file()
                ):
                    backup_now = datetime.now(timezone.utc)
                    backup_due = (
                        last_backup_monotonic is None
                        or time.monotonic() - last_backup_monotonic >= backup_interval * 3600
                    )
                    retry_delay = int(state["backup_retry_delay_seconds"])
                    retry_ready = (
                        last_backup_attempt_at is None
                        or (backup_now - last_backup_attempt_at).total_seconds() >= retry_delay
                    )
                    if backup_due and retry_ready:
                        last_backup_attempt_at = backup_now
                        try:
                            backup_result = backup_function(
                                [paper_database, database],
                                chosen_backup_directory,
                                retain=retain_backups,
                            )
                            files = backup_result.get("files") or []
                            manifest_name = str(backup_result.get("manifest_name") or "").strip()
                            if backup_result.get("skipped_missing_sources") or len(files) != 2 or not manifest_name:
                                raise RuntimeError("both Perps databases and their verified manifest must be backed up")
                            manifest_time = datetime.fromisoformat(
                                str(backup_result.get("created_at") or "").replace("Z", "+00:00")
                            )
                            if manifest_time.tzinfo is None or manifest_time.utcoffset() is None:
                                raise RuntimeError("Perps backup returned a timezone-naive timestamp")
                            state.update({
                                "last_backup_at": manifest_time.astimezone(timezone.utc).isoformat(),
                                "last_backup_manifest_name": manifest_name[:200],
                                "backup_files_last_run": 2,
                                "backup_error_type": None,
                                "backup_consecutive_failures": 0,
                                "backup_retry_delay_seconds": 0,
                            })
                            last_backup_monotonic = time.monotonic()
                        except Exception as backup_exc:
                            failures = int(state["backup_consecutive_failures"]) + 1
                            delay = min(3600, 60 * (2 ** min(failures - 1, 6)))
                            state.update({
                                "backup_error_type": type(backup_exc).__name__[:80],
                                "backup_consecutive_failures": failures,
                                "backup_retry_delay_seconds": delay,
                            })
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
                if current_engine is not None:
                    state["paper_engine_state"] = "degraded"
                    try:
                        state["paper_engine"] = current_engine.summary()
                    except Exception:
                        state["paper_engine"] = None
                _write_status(status_file, state)
                if max_cycles is not None or failures >= failure_limit:
                    print(
                        f"Perps worker exiting after {failures} consecutive failures: {state['last_error']}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 1
                if stopping.wait(delay):
                    break
        state["worker_state"] = "stopped"
        state["last_heartbeat_at"] = _utc_now()
        _write_status(status_file, state)
        return 0
    finally:
        if engine_created and current_engine is not None:
            current_engine.close()
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
