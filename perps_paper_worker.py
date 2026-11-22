"""Supervised public-data Perps paper worker.

This worker refreshes public market data, maintains paper marks/funding,
manages protective exits, and can optionally generate experimental Paper-only
entries when PERPS_AUTO_ENTRY_ENABLED=true. It has no authenticated SDK client,
signer, wallet, or order-submission capability.
"""
from __future__ import annotations

import json
import os
import signal
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backup_paper_data import create_paper_data_backups
from perps_market_data import PerpsMarketDataProvider, provider_from_env
from perps_paper_engine import PerpsPaperEngine
from perps_signal_strategy import strategy_from_env


STATUS_SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent),
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(serialized + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"invalid_{name}") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"invalid_{name}")
    return value


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"invalid_{name}") from None
    import math
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"invalid_{name}")
    return value


def _wait(stopping: Any, seconds: float, *, path: Path, status: dict[str, Any]) -> bool:
    remaining = max(0.0, float(seconds))
    while remaining > 0 and not stopping.is_set():
        chunk = min(15.0, remaining)
        if stopping.wait(chunk):
            return True
        status["updated_at"] = _utc_now()
        _write_status(path, status)
        remaining = max(0.0, remaining - chunk)
    return bool(stopping.is_set())


def read_perps_worker_status(
    path: str | Path,
    *,
    now: datetime | None = None,
    stale_after_seconds: float = 30.0,
) -> dict[str, Any]:
    """Read an allowlisted worker status; raw JSON is never exposed."""
    target = Path(path).expanduser()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != STATUS_SCHEMA_VERSION:
            raise ValueError("unsupported_schema")
        state = raw.get("worker_state")
        if state not in {"starting", "running", "degraded", "failed", "stopped"}:
            raise ValueError("invalid_worker_state")
        updated = _parse_utc(raw.get("updated_at"))
        if updated is None:
            raise ValueError("invalid_heartbeat")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now_must_be_timezone_aware")
        age = (current.astimezone(timezone.utc) - updated).total_seconds()
        allowed = {
            "schema_version": STATUS_SCHEMA_VERSION,
            "worker_state": state,
            "updated_at": updated.isoformat(),
            "started_at": _parse_utc(raw.get("started_at")).isoformat()
            if _parse_utc(raw.get("started_at")) else None,
            "last_cycle_at": _parse_utc(raw.get("last_cycle_at")).isoformat()
            if _parse_utc(raw.get("last_cycle_at")) else None,
            "last_success_at": _parse_utc(raw.get("last_success_at")).isoformat()
            if _parse_utc(raw.get("last_success_at")) else None,
            "cycles": max(0, int(raw.get("cycles", 0))),
            "consecutive_failures": max(0, int(raw.get("consecutive_failures", 0))),
            "total_failures": max(0, int(raw.get("total_failures", 0))),
            "markets_loaded_last_cycle": max(0, int(raw.get("markets_loaded_last_cycle", 0))),
            "snapshots_processed_last_cycle": max(0, int(raw.get("snapshots_processed_last_cycle", 0))),
            "positions_closed_last_cycle": max(0, int(raw.get("positions_closed_last_cycle", 0))),
            "funding_gaps_last_cycle": max(0, int(raw.get("funding_gaps_last_cycle", 0))),
            "exit_blocks_last_cycle": max(0, int(raw.get("exit_blocks_last_cycle", 0))),
            "last_exit_block_reason": str(raw.get("last_exit_block_reason") or "")[:120] or None,
            "last_error_type": str(raw.get("last_error_type") or "")[:80] or None,
            "last_cycle_warning": str(raw.get("last_cycle_warning") or "")[:120] or None,
            "last_backup_at": _parse_utc(raw.get("last_backup_at")).isoformat()
            if _parse_utc(raw.get("last_backup_at")) else None,
            "backup_error_type": str(raw.get("backup_error_type") or "")[:80] or None,
            "open_positions": max(0, int(raw.get("open_positions", 0))),
            "cash_balance": str(raw.get("cash_balance", ""))[:80],
            "equity": str(raw.get("equity", ""))[:80],
            "mode": "PERPS_PAPER_ONLY",
            "strategy_status": str(raw.get("strategy_status") or "disabled")[:40],
            "strategy_decisions_last_cycle": max(0, int(raw.get("strategy_decisions_last_cycle", 0))),
            "signals_last_cycle": max(0, int(raw.get("signals_last_cycle", 0))),
            "entries_opened_last_cycle": max(0, int(raw.get("entries_opened_last_cycle", 0))),
            "entry_blocks_last_cycle": max(0, int(raw.get("entry_blocks_last_cycle", 0))),
            "strategy_last_reason": str(raw.get("strategy_last_reason") or "")[:120] or None,
            "research_only": True,
            "live_execution_enabled": False,
            "execution": "none",
        }
        allowed["heartbeat_age_seconds"] = round(max(0.0, age), 1) if age >= -5 else None
        allowed["heartbeat_fresh"] = -5 <= age <= stale_after_seconds
        return allowed
    except FileNotFoundError:
        return {
            "schema_version": STATUS_SCHEMA_VERSION,
            "worker_state": "unknown",
            "heartbeat_fresh": False,
            "research_only": True,
            "live_execution_enabled": False,
            "execution": "none",
            "status_error": "status_file_missing",
        }
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "schema_version": STATUS_SCHEMA_VERSION,
            "worker_state": "unknown",
            "heartbeat_fresh": False,
            "research_only": True,
            "live_execution_enabled": False,
            "execution": "none",
            "status_error": "status_file_invalid",
        }


def run_worker(
    *,
    provider_factory: Callable[[], PerpsMarketDataProvider] = provider_from_env,
    engine_factory: Callable[[], PerpsPaperEngine] | None = None,
    stop_event: Any | None = None,
    status_path: str | Path | None = None,
    poll_seconds: int | None = None,
    max_consecutive_failures: int = 5,
    backup_function: Callable[..., dict[str, Any]] | None = create_paper_data_backups,
    backup_interval_hours: float | None = None,
    backup_directory: str | Path | None = None,
    backup_retain: int | None = None,
) -> int:
    """Refresh public Perps data, update paper state, retry, and back up SQLite."""
    interval = poll_seconds if poll_seconds is not None else _int_env("PERPS_POLL_SECONDS", 5, 1, 60)
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 60:
        raise ValueError("PERPS_POLL_SECONDS must be an integer between 1 and 60")
    if isinstance(max_consecutive_failures, bool) or not isinstance(max_consecutive_failures, int) or max_consecutive_failures < 1:
        raise ValueError("max_consecutive_failures must be a positive integer")
    backup_hours = backup_interval_hours if backup_interval_hours is not None else _float_env(
        "PERPS_BACKUP_INTERVAL_HOURS", 6.0, 1.0, 168.0
    )
    if not 1 <= float(backup_hours) <= 168:
        raise ValueError("PERPS_BACKUP_INTERVAL_HOURS must be between 1 and 168")
    retain = backup_retain if backup_retain is not None else _int_env(
        "PERPS_BACKUP_RETAIN", 30, 1, 365
    )
    if isinstance(retain, bool) or not isinstance(retain, int) or not 1 <= retain <= 365:
        raise ValueError("PERPS_BACKUP_RETAIN must be between 1 and 365")
    stopping = stop_event or threading.Event()
    target = Path(status_path or os.getenv(
        "PERPS_PAPER_WORKER_STATUS_FILE", "./data/perps_paper_worker_status.json"
    )).expanduser()
    started = _utc_now()
    db_path = Path(os.getenv(
        "PERPS_PAPER_DATABASE_PATH", "./data/perps_paper.sqlite3"
    )).expanduser()
    backup_path = Path(backup_directory or os.getenv(
        "PERPS_BACKUP_DIRECTORY", str(db_path.parent / "backups")
    )).expanduser()
    status: dict[str, Any] = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "worker_state": "starting",
        "started_at": started,
        "updated_at": started,
        "last_cycle_at": None,
        "last_success_at": None,
        "cycles": 0,
        "consecutive_failures": 0,
        "total_failures": 0,
        "markets_loaded_last_cycle": 0,
        "snapshots_processed_last_cycle": 0,
        "positions_closed_last_cycle": 0,
        "funding_gaps_last_cycle": 0,
        "exit_blocks_last_cycle": 0,
        "last_exit_block_reason": None,
        "last_error_type": None,
        "last_cycle_warning": None,
        "last_backup_at": None,
        "backup_error_type": None,
        "open_positions": 0,
        "cash_balance": None,
        "equity": None,
        "mode": "PERPS_PAPER_ONLY",
        "strategy_status": "disabled",
        "strategy_decisions_last_cycle": 0,
        "signals_last_cycle": 0,
        "entries_opened_last_cycle": 0,
        "entry_blocks_last_cycle": 0,
        "strategy_last_reason": None,
        "research_only": True,
        "live_execution_enabled": False,
        "execution": "none",
    }
    _write_status(target, status)
    engine = None
    exit_code = 0
    backup_due_at = 0.0
    backup_failures = 0
    strategy = None
    try:
        auto_entry_raw = os.getenv("PERPS_AUTO_ENTRY_ENABLED", "false").strip().lower()
        if auto_entry_raw not in {"true", "false"}:
            raise ValueError("PERPS_AUTO_ENTRY_ENABLED must be exactly true or false")
        auto_entry_enabled = auto_entry_raw == "true"
        if auto_entry_enabled:
            strategy = strategy_from_env()
            status["strategy_status"] = "warming_up"
        else:
            status["strategy_status"] = "disabled"
        provider = provider_factory()
        if engine_factory is None:
            engine = PerpsPaperEngine(
                db_path,
                starting_cash=os.getenv("PERPS_PAPER_STARTING_CASH", "100"),
                taker_fee_rate=os.getenv("PERPS_PAPER_TAKER_FEE_RATE", "0.0005"),
                max_entry_impact_bps=os.getenv("PERPS_MAX_ENTRY_IMPACT_BPS", "100"),
                max_market_data_age_seconds=os.getenv("PERPS_MAX_MARKET_AGE_SECONDS", "10"),
            )
        else:
            engine = engine_factory()
        backup_due_at = time.monotonic()
        while not stopping.is_set():
            cycle_started = _utc_now()
            try:
                snapshots = provider.load_snapshots()
                if not snapshots:
                    raise RuntimeError("no_perps_market_snapshots")
                closed = 0
                funding_gaps = 0
                exit_blocks = 0
                last_exit_block_reason = None
                processed = 0
                strategy_decisions = 0
                signals_seen = 0
                entries_opened = 0
                entry_blocks = 0
                strategy_last_reason = None
                strategy_cycle_status = "warming_up" if strategy is not None else "disabled"
                for snapshot in snapshots:
                    outcome = engine.process_snapshot(snapshot)
                    processed += 1
                    if outcome.get("status") == "PAPER_POSITION_CLOSED":
                        closed += 1
                        if outcome.get("funding_reconciliation") == "incomplete":
                            funding_gaps += 1
                    elif outcome.get("status") == "PAPER_POSITION_FUNDING_GAP":
                        funding_gaps += 1
                    elif outcome.get("status") == "PAPER_EXIT_BLOCKED":
                        exit_blocks += 1
                        if outcome.get("funding_reconciliation") == "incomplete":
                            funding_gaps += 1
                        last_exit_block_reason = str(outcome.get("reason") or "exit_fill_blocked")[:120]

                    if strategy is not None:
                        instrument_id = int(snapshot["instrument_id"])
                        history = engine.recent_snapshots(instrument_id, limit=12000)
                        strategy_now = datetime.now(timezone.utc)
                        decision = strategy.evaluate(
                            history,
                            current_snapshot=snapshot,
                            now=strategy_now,
                        )
                        decision_reason = str(decision.get("reason") or "unknown")
                        if decision_reason == "warming_up_insufficient_completed_bars":
                            strategy_cycle_status = "warming_up"
                        elif decision_reason in {"incomplete_contiguous_minute_history", "completed_bar_is_stale"}:
                            strategy_cycle_status = "degraded"
                        else:
                            strategy_cycle_status = "active"
                        if decision.get("reason") not in {
                            "warming_up_insufficient_completed_bars",
                            "incomplete_contiguous_minute_history",
                            "completed_bar_is_stale",
                            "snapshot_not_research_only",
                            "instrument_not_entry_eligible",
                            "instrument_settled",
                        }:
                            bar_close_ms = decision.get("bar_close_ms")
                            if isinstance(bar_close_ms, int) and engine.claim_strategy_bar(
                                instrument_id, bar_close_ms, now=strategy_now
                            ):
                                strategy_decisions += 1
                                strategy_last_reason = str(decision.get("reason") or "unknown")[:120]
                                if decision.get("signal") is True:
                                    signals_seen += 1
                                    entry = engine.open_position(
                                        snapshot,
                                        side=str(decision["side"]),
                                        requested_notional=decision["entry_notional_usd"],
                                        now=strategy_now,
                                        stop_loss_distance_pct=decision["stop_loss_distance_pct"],
                                        reward_risk_multiple=decision["reward_risk_multiple"],
                                    )
                                    if entry.get("status") == "PAPER_POSITION_OPEN":
                                        entries_opened += 1
                                        engine.record_strategy_decision(
                                            instrument_id,
                                            bar_close_ms,
                                            decision_status="entry_opened",
                                            reason=str(decision.get("reason") or "signal"),
                                            side=str(decision["side"]),
                                            trade_id=str(entry["trade_id"]),
                                            metrics=decision,
                                            now=strategy_now,
                                        )
                                    else:
                                        entry_blocks += 1
                                        engine.record_strategy_decision(
                                            instrument_id,
                                            bar_close_ms,
                                            decision_status="entry_blocked",
                                            reason=str(entry.get("reason") or "paper_risk_gate_blocked"),
                                            side=str(decision["side"]),
                                            metrics={
                                                "signal": decision,
                                                "risk_gate": entry.get("risk_gate"),
                                                "fill": entry.get("fill"),
                                            },
                                            now=strategy_now,
                                        )
                                else:
                                    engine.record_strategy_decision(
                                        instrument_id,
                                        bar_close_ms,
                                        decision_status="no_signal",
                                        reason=str(decision.get("reason") or "no_signal"),
                                        metrics=decision,
                                        now=strategy_now,
                                    )
                metrics = engine.account_metrics()
                warning = (
                    "protective_exit_blocked" if exit_blocks
                    else "funding_reconciliation_incomplete" if funding_gaps
                    else None
                )
                status.update({
                    "worker_state": "degraded" if warning or status.get("backup_error_type") else "running",
                    "last_cycle_at": cycle_started,
                    "last_success_at": _utc_now(),
                    "cycles": status["cycles"] + 1,
                    "consecutive_failures": 0,
                    "markets_loaded_last_cycle": len(snapshots),
                    "snapshots_processed_last_cycle": processed,
                    "positions_closed_last_cycle": closed,
                    "funding_gaps_last_cycle": funding_gaps,
                    "exit_blocks_last_cycle": exit_blocks,
                    "last_exit_block_reason": last_exit_block_reason,
                    "strategy_status": strategy_cycle_status,
                    "strategy_decisions_last_cycle": strategy_decisions,
                    "signals_last_cycle": signals_seen,
                    "entries_opened_last_cycle": entries_opened,
                    "entry_blocks_last_cycle": entry_blocks,
                    "strategy_last_reason": strategy_last_reason,
                    "last_error_type": None,
                    "last_cycle_warning": warning,
                    "open_positions": int(metrics["open_positions"]),
                    "cash_balance": str(metrics["cash_balance"]),
                    "equity": str(metrics["equity"]),
                    "updated_at": _utc_now(),
                })
                now_mono = time.monotonic()
                if backup_function is not None and now_mono >= backup_due_at:
                    try:
                        backup_result = backup_function(
                            [engine.database_path],
                            backup_path,
                            retain=retain,
                        )
                        if len(backup_result.get("files") or []) != 1:
                            raise RuntimeError("perps_database_backup_missing")
                        backup_at = _parse_utc(backup_result.get("created_at"))
                        if backup_at is None:
                            raise RuntimeError("perps_backup_timestamp_missing")
                        status["last_backup_at"] = backup_at.isoformat()
                        status["backup_error_type"] = None
                        backup_failures = 0
                        backup_due_at = now_mono + float(backup_hours) * 3600
                    except Exception as exc:
                        backup_failures += 1
                        status["backup_error_type"] = type(exc).__name__[:80]
                        backup_due_at = now_mono + min(3600.0, 60.0 * (2 ** min(backup_failures - 1, 6)))
                _write_status(target, status)
                if _wait(stopping, interval, path=target, status=status):
                    break
            except Exception as exc:
                failures = int(status["consecutive_failures"]) + 1
                delay = min(300.0, 5.0 * (2 ** min(failures - 1, 6)))
                status.update({
                    "worker_state": "degraded" if failures < max_consecutive_failures else "failed",
                    "last_cycle_at": cycle_started,
                    "consecutive_failures": failures,
                    "total_failures": int(status["total_failures"]) + 1,
                    "last_error_type": type(exc).__name__[:80],
                    "last_cycle_warning": None,
                    "updated_at": _utc_now(),
                })
                _write_status(target, status)
                if failures >= max_consecutive_failures:
                    exit_code = 1
                    break
                if _wait(stopping, delay, path=target, status=status):
                    break
    except Exception as exc:
        exit_code = 1
        status.update({
            "worker_state": "failed",
            "last_error_type": type(exc).__name__[:80],
            "updated_at": _utc_now(),
        })
        _write_status(target, status)
    finally:
        status["worker_state"] = "failed" if exit_code else "stopped"
        status["updated_at"] = _utc_now()
        _write_status(target, status)
    return exit_code


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    stopping = threading.Event()

    def request_shutdown(_signum: int, _frame: Any) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    return run_worker(stop_event=stopping)


if __name__ == "__main__":
    raise SystemExit(main())
